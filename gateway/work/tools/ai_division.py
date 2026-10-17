"""Persistent AI division. Native league_info fields, actual scores, atomic receipts."""
from __future__ import annotations
import copy
import json
import time
import club_store as store
import ziru_data
import owned_coaches
import match_protocol as match
import match_stats

# Difficulty by division. Endpoints are official-corpus facts: 050 capture
# (current_division=10) serves VERYEASY and 145 (current_division=8) serves EASY;
# Other bands retain the local progression policy, using the seven native
# EMenuCpuLevel values. The Superstar display label is HARDEST on the wire.
NATIVE_LEVELS = ('VERYEASY','EASY','NORMAL','HARD','VERYHARD','HARDEST','LEGEND')
LEVELS = {10:'VERYEASY',9:'EASY',8:'EASY',7:'NORMAL',6:'HARD',
          5:'VERYHARD',4:'VERYHARD',3:'HARDEST',2:'HARDEST',1:'LEGEND'}
STATE_KEY = 'ai_division_state_v1'
RULE_VERSION = 2
RATING_START = {3:1300, 2:1400, 1:1500}
RATING_FLOOR = {3:1200, 2:1400, 1:1500}

def initial_points(div):
    return RATING_START.get(div, 0)

def is_league(req):
    return str(req.get('match_category') or '').startswith('LEAGUE_COM')

def rules():
    out=copy.deepcopy(ziru_data.division_rules())
    out.update(schema='local-division-v3',
               matches_per_division={str(d):quota(d) for d in range(1,11)},
               promotion_points={str(d):threshold(d) for d in range(1,11)},
               rating_start=RATING_START.copy(),rating_floor=RATING_FLOOR.copy(),
               relegation_note='D10-D4: 3 games, <=3 relegated, 4-6 stay, >=7 promoted; D3-D1: continuous rating')
    return out

def state(conn):
    raw = store.get_meta(conn, STATE_KEY, '')
    s=json.loads(raw) if raw else dict(division=10,best=10,points=0,matches=0,
                                     wins=0,draws=0,losses=0,round=1)
    if s.get('rule_version') != RULE_VERSION:
        # Old high divisions used round points, never treat those as a rating.
        if s['division']<=3:
            s.update(points=initial_points(s['division']),matches=0,wins=0,draws=0,losses=0)
        elif s['matches']>=3:
            s.update(points=0,matches=0,wins=0,draws=0,losses=0,round=s['round']+1)
        s['rule_version']=RULE_VERSION
    return s

def save(conn, s):
    store.set_meta(conn, STATE_KEY, json.dumps(s))

def threshold(div):
    return {3:1400,2:1500,1:1500}.get(div,7)

def quota(div):
    return 3 if div>=4 else 0

def view(template, s, outcome='', pre_rating=None):
    v = copy.deepcopy(template or {})
    div=s['division']; target=threshold(div)
    rated=div<=3; rating=s['points'] if rated else 0
    v.update(current_division=div,personal_best_division=s['best'],
             personal_best_division_all_phase=s['best'],round_match_count=s['matches'],
             current_point=s['points'],point_promoted=target,
             point_staying=RATING_FLOOR[div] if rated else 6,
             point_relegated=RATING_FLOOR[div] if rated else 3,
             can_skip='YES' if div>1 else 'NO',is_com_match='YES',rating=rating,
             pre_rating=rating if pre_rating is None else pre_rating,
             is_first_enter='NO',is_first_best_division='NO',
             is_division_first_match='YES' if s['matches']==0 else 'NO',
             is_promoted='YES' if outcome=='PROMOTED' else 'NO',
             is_staying='YES' if outcome=='STAYING' else 'NO',
             is_relegated='YES' if outcome=='RELEGATED' else 'NO',simple_ranking=[])
    v['com_level_info']={'offense_com_level':LEVELS[div],'defense_com_level':LEVELS[div],
                         'offense_sub_com_level':'NONE','defense_sub_com_level':'NONE'}
    history=copy.deepcopy(v.get('round_match_result') or {})
    history.update(rank=0,match_history_list=[],win=s['wins'],draw=s['draws'],lose=s['losses'],
                   ratio=100*s['wins']//s['matches'] if s['matches'] else 0)
    v['round_match_result']=history
    return v

def info(conn, resp):
    match.remember_mode(conn, match.AI)
    resp['league_info']=view(resp.get('league_info'),state(conn))
    resp['result']='NOERR'
    return resp

def _begin(conn):
    if conn.in_transaction:
        raise RuntimeError('AI division requires an independent transaction')
    conn.execute('BEGIN IMMEDIATE')

def _reset(s, div, points=None):
    s.update(division=div,best=min(s['best'],div),
             points=initial_points(div) if points is None else points,matches=0,wins=0,draws=0,losses=0,
             round=s['round']+1)

def prepare(conn, resp, req, *, session_token=None):
    # Profiles contain captured ids. Allocate a fresh int64 so each match has a
    # stable receipt key even after the opponent pool wraps around.
    _begin(conn)
    active=None
    try:
        active=json.loads(store.get_meta(conn,'ai_division_active_match','null'))
        s=state(conn)
        active,intent=match.prepare_active_for_request(conn,match.AI,active,req,session_token)
        if not active:
            sequence=match.next_game_id(conn)
            store.set_meta(conn,'ai_division_game_sequence',str(sequence))
            active={'game_id':sequence,'round':s['round'],'division':s['division'],
                    'started':False,'side':'HOME' if resp.get('side','AWAY')=='AWAY' else 'AWAY',
                    'match_category':req.get('match_category'),
                    'opponent':copy.deepcopy(resp)}
        active.update(intent)
        store.set_meta(conn,'ai_division_active_match',json.dumps(active))
        # Exact request retries retain the frozen opponent. A new fetch after
        # an abandoned running match has already allocated a separate identity.
        if active.get('opponent'):
            saved=ziru_data.normalize_opponent_team(copy.deepcopy(active['opponent']))
            owned_coaches.sync_opponent(conn, saved)
            # Old sessions retain their opponent JSON across server restarts.
            # Repair its team assets too, without discarding match progress.
            saved['cpu_level']=LEVELS[active['division']]
            active['opponent']=copy.deepcopy(saved)
            store.set_meta(conn,'ai_division_active_match',json.dumps(active))
            saved.update(rqid=resp.get('rqid'),msgid=resp.get('msgid'))
            resp=saved
        resp['game_id']=active['game_id']
        resp['cpu_level']=LEVELS[active['division']]
        match.remember_mode(conn, match.AI)
        match.diagnostic(conn,'prepared',req,match.AI,active)
        conn.commit();return resp
    except match.MatchError as exc:
        conn.rollback()
        return match.reject(conn,resp.get('msgid','CMD_GET_VSCOM_OPPONENT'),req,exc,match.AI,active)
    except Exception:
        conn.rollback();raise

def start(conn, req, home_entries=None):
    _begin(conn)
    active=None
    try:
        if match.resolve_mode(conn,req,include_receipts=False)!=match.AI:
            raise match.MatchError('wrong_match_mode')
        active=match.active_match(conn,match.AI)
        user_side=match.side(req.get('side') or active['side'])
        already_started=bool(active.get('started'))
        if already_started and user_side!=active['side']:
            raise match.MatchError('started_match_side_changed')
        # Gate passes the exact resolved response roster, including its saved
        # squad fallback. The request alone may omit player_list_home.
        snapshot={}
        match_stats.snapshot_roster(snapshot,home_entries if home_entries is not None
                                    else req.get('player_list_home'))
        previous=active.get('eligible_player_instances')
        current=snapshot.get('eligible_player_instances')
        if already_started and previous and current and previous!=current:
            raise match.MatchError('started_match_roster_changed')
        if not already_started:
            active.update(started=True,side=user_side,user_id=req.get('user_id'))
            active['team_id']=store.club_team_id(conn)
            active['user_name']=store.get_meta(conn,'user_name','')
        # A legacy active match may lack this snapshot. Fill it once; an
        # already captured order must survive retries and process restarts.
        if not previous and current:
            active.update(snapshot)
        store.set_meta(conn,'ai_division_active_match',json.dumps(active))
        match.diagnostic(conn,'started',req,match.AI,active)
        conn.commit();return True
    except match.MatchError as exc:
        conn.rollback()
        match.reject(conn,'CMD_START_GAME',req,exc,match.AI,active)
        return False
    except Exception:
        conn.rollback();raise

def _scores(req):
    # Compatibility for imports; new settlement also keeps score provenance.
    home,away,_=match.scores(req)
    return home,away

def _result_response(resp,req,s,outcome,resolved,active,pre_rating=0):
    # A command-name capture may be from another mode; always use this mode's
    # schema, then fill the current match rather than retaining captured state.
    out=ziru_data.payload('vscom_game_result_template.json')
    out.update(msgid='CMD_GET_VSCOM_GAME_RESULT',rqid=req.get('rqid'),result='NOERR')
    end=out['game_end_result'];result_data=end['match_result']
    normal,abnormal=match.end_reasons(match.AI,resolved)
    result_data.update(game_res=resolved['result'],gameend_reason=normal,abnormalend_reason=abnormal,
                 score_data={'home_score':resolved['home'],'away_score':resolved['away']})
    pdata=result_data.get('player_data') or {};pdata['user_id']=active.get('user_id') or req.get('user_id',0)
    pdata.update(side=resolved['side'],team_id=active.get('team_id',0),cont_win_num=0)
    pdata.update(rating=s['points'] if s['division']<=3 else 0,oldrating=pre_rating)
    if isinstance(pdata.get('user_info'),dict):
        pdata['user_info'].update(user_id=pdata['user_id'],user_name=active.get('user_name',''))
    result_data['player_data']=pdata
    extra=end.setdefault('extra_match_result',{})
    league=extra.setdefault('league_match_result',{})
    league['league_info']=view(league.get('league_info'),s,outcome,pre_rating);league['is_invalid']='NO'
    # Captured rewards and player mutations belong to another match/account.
    for k in ('event_match_result','season_match_result','gameplayer_update_info','gameplayer_exp_list',
              'campaign_pass_result','get_trial_present','ml_event_match_result','user_compe_core_info'):
        extra[k]=[]
    for k in ('achievement_notification','sheet_list','fame_notification','fameIdList',
              'revealed_bingo_link_card_list','empty_slot_info_list'):
        end[k]=[]
    if isinstance(end.get('analyst_get_game_result_info'),dict):
        end['analyst_get_game_result_info'].update(advice_info_match_list=[],analyse_json='[]',advice_json='[]')
    return out

def finish(conn, resp, req):
    mid='CMD_GET_VSCOM_GAME_RESULT';active=None
    _begin(conn)
    try:
        if match.resolve_mode(conn,req)!=match.AI:
            raise match.MatchError('wrong_match_mode')
        gid=match.match_id(req)
        cached=match.replay(conn,match.AI,req)
        if cached is not None:
            conn.commit();return cached
        active=match.active_match(conn,match.AI)
        if not active or not active.get('started'):
            raise match.MatchError('result_before_start')
        resolved=match.outcome(req,active)
        result=resolved['result']
        s=state(conn)
        if active['round']!=s['round']:raise match.MatchError('stale_round')
        old=s['division'];rated=old<=3
        pre_rating=s['points'] if rated else 0
        points=({'WIN':30,'DRAW':0,'LOSE':-30} if rated else {'WIN':3,'DRAW':1,'LOSE':0})[result]
        s['points']+=points;s['matches']+=1
        s[{'WIN':'wins','DRAW':'draws','LOSE':'losses'}[result]]+=1
        completed=copy.deepcopy(s);outcome='';div=old
        if rated:
            if s['points']<RATING_FLOOR[old]:div=old+1;outcome='RELEGATED'
            elif old>1 and s['points']>=threshold(old):div=old-1;outcome='PROMOTED'
            if div!=old:
                _reset(s,div,s['points'] if div<=3 else 0)
        elif s['matches']>=quota(old):
            if s['points']>=threshold(old):div=old-1;outcome='PROMOTED'
            elif s['points']<=3 and old<10:div=old+1;outcome='RELEGATED'
            else:outcome='STAYING'
            _reset(s,div)
        save(conn,s)
        out=_result_response(resp,req,s,outcome,resolved,active,pre_rating)
        match_stats.record_result(conn,match.AI,req,active,resolved)
        match_stats.update_response(conn,match.AI,gid,out)
        match.save_receipt(conn,match.AI,req,out,resolved)
        store.set_meta(conn,'ai_division_active_match','null')
        match.diagnostic(conn,'settled',req,match.AI,active,resolved,response=out)
        store.log_event(conn,'ai_division_result',{'game_id':gid,'result':result,'points_added':points,
                         'completed_round':completed,'outcome':outcome,'state':s})
        conn.commit();return out
    except match.MatchError as exc:
        conn.rollback();return match.reject(conn,mid,req,exc,match.AI,active)
    except Exception:
        conn.rollback();raise

def skip(conn, resp, req):
    # Native can_skip exposes the button even at zero points. One click advances
    # one division; retained receipts prevent a retried click advancing twice.
    key='ai_division_skip_'+str(req.get('user_id',0))+'_'+str(req.get('rqid'))
    _begin(conn)
    try:
        cached=store.get_meta(conn,key,'')
        if cached:conn.rollback();return json.loads(cached)
        if json.loads(store.get_meta(conn,'ai_division_active_match','null')):
            conn.rollback();return {'msgid':'CMD_SKIP_LEAGUE_COM_DIVISION','rqid':req.get('rqid'),'result':'ERR_INVALID_PARAM'}
        s=state(conn);old=s['division']
        if old>1:_reset(s,old-1)
        save(conn,s)
        resp['league_info']=view(resp.get('league_info'),s,'PROMOTED' if old>1 else '')
        resp['result']='NOERR'
        store.set_meta(conn,key,json.dumps(resp))
        store.log_event(conn,'ai_division_skip',{'from':old,'to':s['division'],'points_required':0})
        conn.commit();return resp
    except Exception:
        conn.rollback();raise
