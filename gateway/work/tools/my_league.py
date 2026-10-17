"""My League: native team selection, schedule, table and atomic score receipts."""
from __future__ import annotations
import copy,json,time
from collections import defaultdict
import club_store as store
import ziru_data
import owned_coaches
from ai_division import NATIVE_LEVELS
import match_protocol as protocol
import match_stats
import season_rankings
import myleague_plan
import player_release
KEY='my_league_state_v1'
LEVELS=frozenset(NATIVE_LEVELS)

def state(conn):
 s=json.loads(store.get_meta(conn,KEY,'null'))
 if s and 'schedule_v2' not in s:s['schedule_v2']=fixtures(s)
 return s
def save(conn,s):store.set_meta(conn,KEY,json.dumps(s))
def is_season(req):return any(x in str(req.get('match_category','')) for x in ('SEASON','MYLEAGUE','MY_LEAGUE'))
def begin(conn):
 if conn.in_transaction:raise RuntimeError('My League requires independent transaction')
 conn.execute('BEGIN IMMEDIATE')
def fail(mid,req):return {'msgid':mid,'rqid':req.get('rqid'),'result':'ERR_INVALID_PARAM'}
def fixtures(s):
 if 'schedule_v2' in s:return copy.deepcopy(s['schedule_v2'])
 rows=copy.deepcopy(ziru_data.payload(f"schedules/ml_schedule_{s['competition_id']}.json")['schedule_list'])
 # Keep opponents/rounds, but balance venues. Pin all played rounds and any
 # already previewed match; the return leg must retain the opposite venue.
 locked={};first_round={}
 for fx in rows:
  for m in fx['match_list']:
   first_round.setdefault(tuple(sorted((m['home_team_id'],m['away_team_id']))),fx['fixture'])
 def pin(fixture,home,away):
  pair=tuple(sorted((home,away)))
  locked[pair]=(home,away) if fixture==first_round[pair] else (away,home)
 for fixture,saved in s.get('played',{}).items():
  for m in saved['matches']:
   pin(int(fixture),m['home_team_id'],m['away_team_id'])
 active=s.get('active')
 if active:
  for fx in rows:
   if fx['fixture']==active['fixture']:
    for m in fx['match_list']:
     h,a=m['home_team_id'],m['away_team_id']
     if s['team_id'] in (h,a) and (h==s['team_id'])!=(active['side']=='HOME'):h,a=a,h
     pin(fx['fixture'],h,a)
 history=defaultdict(list);venues={}
 def cost(home,away):
  score=0
  for team,side in ((home,'H'),(away,'A')):
   previous=history[team]
   score+=1000 if previous[-2:]==[side,side] else 10 if previous and previous[-1]==side else 0
   score+=previous.count(side)-previous.count('A' if side=='H' else 'H')
  return score
 for fx in rows:
  for m in fx['match_list']:
   h,a=m['home_team_id'],m['away_team_id'];pair=tuple(sorted((h,a)))
   if pair in venues:h,a=venues[pair][::-1]
   else:
    if pair in locked:h,a=locked[pair]
    elif cost(a,h)<cost(h,a):h,a=a,h
    venues[pair]=(h,a)
   m.update(home_team_id=h,away_team_id=a)
   history[h].append('H');history[a].append('A')
 return rows
def current(s):
 rows=fixtures(s)
 if s['completed']:raise ValueError('season completed')
 fx=rows[s['fixture']-1]
 m=next((m for m in fx['match_list'] if s['team_id'] in (m['home_team_id'],m['away_team_id'])),None)
 if m is None:raise ValueError('selected team missing from fixture')
 return fx,m,'HOME' if m['home_team_id']==s['team_id'] else 'AWAY'
def fixture_matches(s,fx):
 """Current-round CPU results, without advancing the season on menu reads.

 The native season-info reply includes the other clubs' simulated scores for
 this round before our match is played. Only our still-pending match keeps a
 0:0 placeholder. These same CPU results are committed with our final score.
 """
 saved=s['played'].get(str(fx['fixture']))
 if saved:return copy.deepcopy(saved['matches'])
 matches=copy.deepcopy(fx['match_list'])
 for match in matches:
  if s['team_id'] in (match['home_team_id'],match['away_team_id']):
   match.update(home_goals=0,away_goals=0)
  else:
   seed=s['season']*31+fx['fixture']*17+match['home_team_id']*7+match['away_team_id']
   match.update(home_goals=seed%4,away_goals=(seed//7)%4)
 return matches

def advance_byes(conn,s):
 """Commit CPU-only bye rounds inside the caller's season transaction.

 A bye is no match for the selected club: never give it a 0:0, result enum,
 reward or player appearance. The other clubs' existing deterministic games
 still belong in the table and season scorer/assist lists exactly once.
 """
 if not conn.in_transaction:raise RuntimeError('My League bye requires a transaction')
 rows=fixtures(s);changed=False
 while not s['completed']:
  fx=rows[s['fixture']-1]
  if any(s['team_id'] in (m['home_team_id'],m['away_team_id']) for m in fx['match_list']):break
  if (s.get('active') or {}).get('started'):raise protocol.MatchError('stale_fixture')
  old=s['played'].get(str(fx['fixture']))
  if old and not old.get('bye'):raise protocol.MatchError('stale_fixture')
  matches=fixture_matches(s,fx)
  season_rankings.record_fixture(conn,s,fx['fixture'],matches)
  s['played'][str(fx['fixture'])]=dict(bye=True,matches=matches)
  s['active']=None
  if not old:store.log_event(conn,'my_league_bye',dict(season=s['season'],fixture=fx['fixture']))
  changed=True
  if fx['fixture']==len(rows):s['completed']=True;break
  s['fixture']=fx['fixture']+1
 if changed:save(conn,s)
 return changed

def _info_fixture(s,rows):
 """Read a playable opponent without advancing an old save on a menu read."""
 idx=min(s['fixture'],len(rows))-1
 def own(fx):return next((m for m in fx['match_list'] if s['team_id'] in (m['home_team_id'],m['away_team_id'])),None)
 match=own(rows[idx])
 if match:return idx,rows[idx],match
 if not s['completed']:
  for next_idx in range(idx+1,len(rows)):
   match=own(rows[next_idx])
   if match:return next_idx,rows[next_idx],match
 # A final bye still displays its real CPU round. Side/opponent metadata is
 # taken from the last actual selected-club fixture, not a fabricated game.
 for previous in reversed(rows[:idx+1]):
  match=own(previous)
  if match:return idx,rows[idx],match
 raise ValueError('selected team missing from season')

def ranking(s):
 teams={v for fx in fixtures(s) for m in fx['match_list'] for v in (m['home_team_id'],m['away_team_id'])}
 table={t:dict(rank=0,pre_rank=0,team_id=t,winning_points=0,wins=0,losses=0,draws=0,goals=0,goals_against=0,diff=0) for t in teams}
 for r in s['played'].values():
  for m in r['matches']:
   h,a=m['home_goals'],m['away_goals']
   for tid,gf,ga in ((m['home_team_id'],h,a),(m['away_team_id'],a,h)):
    row=table[tid];row['goals']+=gf;row['goals_against']+=ga;row['diff']+=gf-ga
    row['wins']+=gf>ga;row['draws']+=gf==ga;row['losses']+=gf<ga;row['winning_points']+=3 if gf>ga else 1 if gf==ga else 0
 out=sorted(table.values(),key=lambda x:(-x['winning_points'],-x['diff'],-x['goals'],x['team_id']))
 for n,row in enumerate(out,1):row['rank']=n
 return out

def season_special(s):
 """The native season plan is separate from ordinary Dream Team game plans."""
 saved=(s or {}).get('special_squad')
 if not isinstance(saved,dict) or saved.get('season')!=(s or {}).get('season'):return None
 data=saved.get('data')
 return data if isinstance(data,dict) and isinstance(data.get('squad_data'),dict) else None

def _clear_released_refs(conn,data):
 """A saved season lineup must never resurrect a card released elsewhere."""
 out=copy.deepcopy(data)
 sd=out.get('squad_data') or {}
 if isinstance(sd.get('player_list'),list):
  # Identity migration predates season plans and does not rewrite this meta
  # record. Resolve only known player aliases, before missing-card cleanup.
  sd['player_list']=store.player_identity.canonical_request(conn,sd['player_list'])
 for slot in (out.get('squad_data') or {}).get('player_list') or []:
  identity=slot.get('gameplayer_id') if isinstance(slot,dict) else None
  if isinstance(identity,dict) and identity.get('id') and not store.player_by_key(conn,identity.get('id'),identity.get('serial')):
   slot['gameplayer_id']={'id':0,'serial':0}
 return out

def own_special(conn,template,team):
 out=copy.deepcopy(template or {});s=state(conn);saved=season_special(s)
 if saved:
  out.update(_clear_released_refs(conn,saved))
 else:
  chosen=selected_squad(conn,{})
  out.update(squad_data=copy.deepcopy(chosen.get('squad_data') or {}),
             rental_gameplayer_list=[],initial_settings='YES')
 # CmdSetSeasonEnter's business converter (raw 0x7655704) uses NO to ignore
 # this supplied plan and load the active ordinary squad instead. A returned
 # saved season plan is ready: this is a server state, not a user preference.
 out['initial_settings']='YES'
 out['rental_team_id']=team
 return out

def set_special(conn,resp,req):
 """Persist CMD_SET_MYCLUB_SPECIAL_DREAM_SQUAD_INFO's request schema.

 Native serializers 0x7918778/0x7919fd0 send special_squad_data, whereas the
 season entry response calls it special_dream_squad_data. An ordinary squad
 save does not receive edits made in the native My League game-plan screen.
 """
 mid='CMD_SET_MYCLUB_SPECIAL_DREAM_SQUAD_INFO';s=state(conn)
 event=req.get('event_id',0)
 if type(event) is not int or not 0<=event<2**31:return fail(mid,req)
 # The same native command is shared by other events. Only the selected season
 # owns this local plan; its first save binds the event ID for this season.
 if not s or store.get_meta(conn,protocol.CONTEXT_KEY,'')!=protocol.SEASON:
  return copy.deepcopy(resp)
 previous=s.get('special_squad') or {}
 if previous.get('season')==s['season'] and previous.get('event_id')!=event:
  store.log_event(conn,'my_league_special_ignored',dict(reason='different_event',event_id=event))
  conn.commit();return copy.deepcopy(resp)
 patch=req.get('special_squad_data')
 if not isinstance(patch,dict) or not isinstance(patch.get('squad_data'),dict):return fail(mid,req)
 begin(conn)
 try:
  s=state(conn);current=own_special(conn,{},s['team_id'])
  sd=copy.deepcopy(current.get('squad_data') or {})
  sd.update(copy.deepcopy(patch['squad_data']))
  refs=sd.get('player_list')
  if not isinstance(refs,list) or len(refs)>40:raise protocol.MatchError('invalid_special_squad_roster')
  used=set()
  for slot in refs:
   identity=slot.get('gameplayer_id') if isinstance(slot,dict) else None
   if not isinstance(identity,dict):raise protocol.MatchError('invalid_special_squad_roster')
   if identity.get('id')==0:continue
   key=(protocol.positive_id(identity.get('id')),protocol.positive_id(identity.get('serial')))
   if key in used or not store.player_by_key(conn,*key):raise protocol.MatchError('unowned_or_duplicate_special_squad_player')
   used.add(key)
  if not owned_coaches.resolve(conn,sd.get('coach')):raise protocol.MatchError('unknown_special_squad_coach')
  if not isinstance(sd.get('squad_gameplan'),dict) or not sd['squad_gameplan']:
   raise protocol.MatchError('missing_special_squad_gameplan')
  current.update(copy.deepcopy(patch));current['squad_data']=sd
  # The selected league club controls the shirt, rather than a captured or
  # different event's rental team. Player parameters remain in the owned store.
  current['rental_team_id']=s['team_id']
  s['special_squad']=dict(season=s['season'],event_id=event,data=current)
  save(conn,s)
  store.log_event(conn,'my_league_special_saved',dict(season=s['season'],event_id=event,
                  slots=len(refs),owned_players=len(used),fields=sorted(str(k) for k in req)[:30]))
  conn.commit()
  out=copy.deepcopy(resp);out.update(msgid=mid,rqid=req.get('rqid'),result='NOERR');return out
 except protocol.MatchError as exc:
  conn.rollback();return protocol.reject(conn,mid,req,exc,protocol.SEASON)
 except Exception:conn.rollback();raise

def root_view(conn,resp):
 """Use the captured neutral selection sentinel until a real team is chosen."""
 out=copy.deepcopy(resp);root=out.get('season_root_info')
 if not isinstance(root,dict):return out
 s=state(conn)
 if s:
  root.update(team_id=s['team_id'],league_compe_unit_id=s['competition_id'],
              league_team_id_list=sorted(row['team_id'] for row in ranking(s)))
 else:
  # Measured in CMD_GET_MAINMENU_MATCH_CREATIVE_INFO: zero means no selected
  # team, 65535 means no selected league. The native client builds its picker.
  root.update(team_id=0,league_compe_unit_id=65535,league_team_id_list=[])
 return out

def selected_squad(conn,req):
 """Resolve the requested saved game plan without falling back to a stranger."""
 requested=req.get('squad_id')
 # CMD_START_GAME's real native serializer has no squad_id. My League uses
 # its special plan, last saved by CMD_SET_MYCLUB_SPECIAL_DREAM_SQUAD_INFO.
 if requested is None or (type(requested) is int and requested==0):
  special=season_special(state(conn))
  if special:return {'squad_data':_clear_released_refs(conn,special)['squad_data']}
 try:sid=store.main_squad_id(conn) if requested is None or (type(requested) is int and requested==0) else protocol.positive_id(requested)
 except protocol.MatchError:raise protocol.MatchError('invalid_squad_id')
 chosen=next((sq for sq in store.squads(conn) if sq.get('squad_id')==sid),None)
 if not chosen:raise protocol.MatchError('unknown_match_squad')
 return chosen

def start_refs(conn,req):
 """Use one saved season plan for both players and tactical slot indices.

 Like the independent reference server, START does not replace a separately
 saved special plan with the client's cached player references. The native
 SET_SPECIAL command persists edits to that plan before START. Its serializer
 has no squad_id either; the ordinary main plan is only the legacy fallback.
 """
 if 'player_list_home' in req and not isinstance(req['player_list_home'],list):
  raise protocol.MatchError('invalid_match_squad')
 # Only confirmed release tombstones may clear stale saved references here.
 # Unknown cards still fail even under a legacy substitution identity policy.
 special=season_special(state(conn))
 squad={'squad_data':special['squad_data']} if special else selected_squad(conn,{})
 saved=(squad.get('squad_data') or {}).get('player_list')
 if not isinstance(saved,list) or len(saved)>40:raise protocol.MatchError('invalid_saved_match_squad')
 saved=store.player_identity.canonical_request(conn,saved)
 refs=[];used=set()
 for slot in saved:
  if not isinstance(slot,dict):raise protocol.MatchError('invalid_saved_match_squad')
  identity=slot.get('gameplayer_id')
  if not isinstance(identity,dict):raise protocol.MatchError('invalid_saved_match_squad')
  if type(identity.get('id')) is int and type(identity.get('serial')) is int and identity['id']==identity['serial']==0:continue
  try:key=(protocol.positive_id(identity.get('id')),protocol.positive_id(identity.get('serial')))
  except protocol.MatchError:raise protocol.MatchError('invalid_saved_match_squad')
  owned=store.player_by_key(conn,*key)
  # selected_squad clears this same confirmed release for the gameplan. The
  # plan validator still rejects a lost starter; an unused bench stays empty.
  if not owned and player_release.is_released(conn,*key):continue
  if key in used or not owned:
   raise protocol.MatchError('unowned_or_duplicate_match_squad_player')
  used.add(key)
  refs.append({'gameplayer_id':copy.deepcopy(identity)})
 if not refs:raise protocol.MatchError('empty_match_squad')
 return refs

def selection_rejected(conn,req,reason):
 # Record field names rather than a request body/account data. This identifies
 # a native selection schema we have not captured without inventing a team.
 store.log_event(conn,'my_league_selection_rejected',dict(reason=reason,
                 fields=sorted(str(k)[:60] for k in req)[:40]))
 conn.commit()
 return fail('CMD_SET_SEASON_ENTER',req)

def select(conn,resp,req):
 mid='CMD_SET_SEASON_ENTER';team=req.get('select_team_id')
 # Native re-entry sends zero team/competition with both continuation flags NO.
 # Zero is a resume sentinel here, not a new team selection.
 existing=state(conn)
 if existing and (team is None or (type(team) is int and team == 0)):
  begin(conn)
  try:
   advance_byes(conn,existing)
   season_rankings.repair_missing_fixtures(conn,existing)
   out=enter_view(conn,existing,req);conn.commit();return out
  except Exception:conn.rollback();raise
 mapping=ziru_data.competition_registry()['team_to_competition']
 comp=req.get('select_compe_unit_id')
 if type(team) is not int or str(team) not in mapping:return selection_rejected(conn,req,'unknown_selected_team')
 comp=comp or mapping[str(team)]
 if type(comp) is not int or comp!=mapping[str(team)]:return selection_rejected(conn,req,'team_league_mismatch')
 s=dict(team_id=team,competition_id=comp,fixture=1,played={},completed=False,active=None,cpu_level='NORMAL',season=1+(existing or {}).get('season',0))
 try:
  available=fixtures(s)
  if not available or not any(team in (m['home_team_id'],m['away_team_id']) for fx in available for m in fx['match_list']):
   raise ValueError('selected team missing from season')
 except (ValueError,IndexError,ziru_data.NotPorted):return selection_rejected(conn,req,'selected_team_schedule_unavailable')
 # Native team selection resets the season once; transport retries reuse receipt.
 key='my_league_select_'+str(req.get('user_id',0))+'_'+str(req.get('rqid'))
 fingerprint=json.dumps([team,comp])
 begin(conn)
 try:
  cached=store.get_meta(conn,key,'') if req.get('rqid') is not None else ''
  if cached:
   record=json.loads(cached);conn.rollback()
   return record['response'] if record['fingerprint']==fingerprint else fail(mid,req)
  existing=state(conn)
  if existing and existing['team_id']==team and not existing['completed']:
   # Native re-entry may send the selection again with a fresh rqid.
   # Resume rather than replacing the schedule, points or active match.
   advance_byes(conn,existing)
   season_rankings.repair_missing_fixtures(conn,existing)
   out=enter_view(conn,existing,req)
   if req.get('rqid') is not None:store.set_meta(conn,key,json.dumps(dict(fingerprint=fingerprint,response=out)))
   conn.commit();return out
  if existing and (existing.get('active') or {}).get('started'):
   conn.rollback();return fail(mid,req)
  # A previewed opponent is not a started match: selecting another club may
  # discard that preview, otherwise it permanently blocks the selection screen.
  s['season']=1+(existing or {}).get('season',0)
  s['schedule_v2']=fixtures(s)
  advance_byes(conn,s)
  save(conn,s);out=enter_view(conn,s,req)
  if req.get('rqid') is not None:store.set_meta(conn,key,json.dumps(dict(fingerprint=fingerprint,response=out)))
  store.log_event(conn,'my_league_select',dict(team=team,competition=comp,season=s['season']))
  conn.commit();return out
 except Exception:conn.rollback();raise

def enter_view(conn,s,req):
 protocol.remember_mode(conn,protocol.SEASON)
 out=ziru_data.season_enter();out.update(msgid='CMD_SET_SEASON_ENTER',rqid=req.get('rqid'),result='NOERR',team_id=s['team_id'],league_compe_unit_id=s['competition_id'],myleague_point=store.season_points(conn),return_myleague_point=0)
 out['special_dream_squad_data']=own_special(conn,out.get('special_dream_squad_data'),s['team_id']);return out

def info(conn,resp,req):
 protocol.remember_mode(conn,protocol.SEASON)
 s=state(conn);out=copy.deepcopy(resp)
 rows=fixtures(s);idx,fx,m=_info_fixture(s,rows)
 home=m['home_team_id']==s['team_id']
 history=[s['played'][str(i)]['result'] for i in sorted(map(int,s['played'])) if not s['played'][str(i)].get('bye') and s['played'][str(i)].get('result') in ('WIN','DRAW','LOSE')]
 out.update(team_id=s['team_id'],fixture=idx+1,compe_unit_id=s['competition_id'],user_side='HOME' if home else 'AWAY',opponent_team_id=m['away_team_id'] if home else m['home_team_id'],ranking=ranking(s),auto_match_list=copy.deepcopy(fx['match_list']),match_history_list=history,myleague_point=store.season_points(conn),is_end_season='YES' if s['completed'] else 'NO',is_end_season_first_access='NO',added_point_list=[],goal_ranking=[],assist_ranking=[],is_success_mission='NO',is_failure_mission='NO',expired_item_list=[],using_item_list=[],rqid=req.get('rqid'),result='NOERR')
 out['auto_match_list']=fixture_matches(s,fx)
 # Clear captured progress: missions have not been accepted in this local season.
 if isinstance(out.get('mission'),dict):out['mission'].update(current_achieve_num=0,reward_point=0,reward_present=[])
 out.update(success_mission_num=0,failure_mission_num=0,boss_match_point=0,is_boss_match='NO',suspended_player_list=[],new_release_player_list=[],fame_notification_list=[],fameIdList=[])
 season_rankings.populate(conn,s,out)
 return out

def get_schedule(conn,resp,req):
 s=state(conn);out=copy.deepcopy(resp);out['schedule_list']=fixtures(s)
 for fx in out['schedule_list']:
  saved=s['played'].get(str(fx['fixture']))
  if saved:fx['match_list']=copy.deepcopy(saved['matches'])
  else:
   for m in fx['match_list']:m.update(home_goals=0,away_goals=0)
 return out

def prepare(conn,req,*,session_token=None):
 begin(conn)
 try:
  s=state(conn)
  if not s:raise protocol.MatchError('season_not_selected')
  advance_byes(conn,s)
  season_rankings.repair_missing_fixtures(conn,s)
  if s['completed']:
   conn.commit();return fail('CMD_GET_VSCOM_OPPONENT',req)
  fx,m,side=current(s)
  # A new opponent fetch after a hard exit may restart this same fixture with
  # a new match ID; it must not preserve an abandoned game's frozen squad.
  # An exact request retry and an unstarted preview keep their original ID.
  requested=req.get('cpu_level')
  if requested is not None and (not isinstance(requested,str) or requested not in LEVELS):
   conn.rollback();return fail('CMD_GET_VSCOM_OPPONENT',req)
  cached=(s.get('active') or {}).get('opponent') or {}
  level=requested if requested is not None else cached.get('cpu_level') or s.get('cpu_level','NORMAL')
  # Compatibility repair applies only to legacy persisted values. Do not
  # advertise or accept SUPERSTAR as an eighth protocol enum.
  if requested is None and level=='SUPERSTAR':level='HARDEST'
  if not isinstance(level,str) or level not in LEVELS:
   conn.rollback();return fail('CMD_GET_VSCOM_OPPONENT',req)
  s['active'],intent=protocol.prepare_active_for_request(conn,protocol.SEASON,s.get('active'),req,session_token)
  if s.get('active') and s['active'].get('started'):
   frozen=s['active']['opponent'].get('cpu_level') or s.get('cpu_level','NORMAL')
   if frozen=='SUPERSTAR':frozen='HARDEST'
   if requested is not None and requested!=frozen:
    raise protocol.MatchError('started_match_difficulty_changed')
   level=frozen
  if not s.get('active'):
   game=protocol.next_game_id(conn)
   store.set_meta(conn,'my_league_sequence',str(game))
   out=ziru_data.ml_get_vscom_opponent();out.update(game_id=game,cpu_level=level,side='AWAY' if side=='HOME' else 'HOME')
   opponent=m['away_team_id'] if side=='HOME' else m['home_team_id']
   out['team_data'].update(base_team_id=opponent,team_name=ziru_data.team_label(opponent),team_name_short=(ziru_data.team_names().get(str(opponent))or{}).get('short',''))
   # The native season opponent uses built-in team rosters (null player/squad data).
   s['active']=dict(game_id=game,fixture=s['fixture'],side=side,started=False,opponent=out,
                    match_category=req.get('match_category'))
  s['active'].update(intent)
  season_rankings.freeze_context(s['active'],s)
  s['cpu_level']=level
  s['active']['opponent']['cpu_level']=level
  save(conn,s)
  out=copy.deepcopy(s['active']['opponent']);out.update(rqid=req.get('rqid'),msgid=req.get('msgid','CMD_GET_VSCOM_OPPONENT'),result='NOERR')
  protocol.remember_mode(conn,protocol.SEASON)
  protocol.diagnostic(conn,'prepared',req,protocol.SEASON,s['active'])
  conn.commit();return out
 except (ValueError,IndexError) as exc:
  conn.rollback();return protocol.reject(conn,'CMD_GET_VSCOM_OPPONENT',req,exc,protocol.SEASON)
 except Exception:conn.rollback();raise

def start(conn,resp,req,home_entries):
 begin(conn)
 a=None
 try:
  if protocol.resolve_mode(conn,req,include_receipts=False)!=protocol.SEASON:
   raise protocol.MatchError('wrong_match_mode')
  s=state(conn);a=s.get('active') if s else None
  if not home_entries:raise protocol.MatchError('empty_match_squad')
  chosen=selected_squad(conn,{});sd=chosen.get('squad_data') or {}
  coach=owned_coaches.resolve(conn,sd.get('coach'))
  if not coach:raise protocol.MatchError('unknown_match_coach')
  if not isinstance(sd.get('squad_gameplan'),dict) or not sd['squad_gameplan']:
   raise protocol.MatchError('missing_match_gameplan')
  gameplan=myleague_plan.gameplan_for_entries(sd,home_entries)
  snapshot={};match_stats.snapshot_roster(snapshot,home_entries)
  if not snapshot.get('eligible_player_instances'):raise protocol.MatchError('empty_match_squad')
  if a.get('started') and a.get('eligible_player_instances') and a['eligible_player_instances']!=snapshot['eligible_player_instances']:
   raise protocol.MatchError('started_match_roster_changed')
  if not a.get('started'):a.update(started=True,user_id=req.get('user_id'),
      request_side=req.get('side') if req.get('side') in ('HOME','AWAY') else None)
  a.update(snapshot)
  save(conn,s)
  out=ziru_data.ml_start_game();out.update(game_id=a['game_id'],rqid=req.get('rqid'),result='NOERR')
  # This protocol's home object is the controlled Dream Team, even when the
  # league schedule marks the selected club AWAY. Unlabelled score-row order
  # retains the existing protocol convention and is logged for phone validation.
  out['squad_data_home']['player_list']=home_entries
  out['squad_data_home']['team_id']=s['team_id']
  out['squad_data_home']['squad_gameplan']=gameplan
  out['squad_data_home']['coach']=coach
  opponent_team=int(a['opponent']['team_data']['base_team_id'])
  # My League represents the explicitly selected league club. Inherited
  # Dream-Team kit choices can still be Barcelona even when the club IDs match.
  out['uniform_list_home'].update(base_team_id=s['team_id'],uniform_list=store.default_uniforms(s['team_id']))
  out['uniform_list_away'].update(base_team_id=opponent_team,uniform_list=store.default_uniforms(opponent_team))
  commentary=out.get('commentary_info')
  if isinstance(commentary,dict) and 'first_match_player_list'in commentary:commentary['first_match_player_list']=[p['gameplayer_id'] for p in home_entries]
  store.log_event(conn,'my_league_start_schema',dict(
      plan_source='special' if season_special(s) else 'ordinary',
      request_roster='empty' if req.get('player_list_home')==[] else 'present' if 'player_list_home' in req else 'omitted',
      resolved_players=len(home_entries),formation_count=len(sd['squad_gameplan'].get('formation') or []),
      selected_team=s['team_id'],opponent_team=opponent_team,scheduled_side=a['side'],
      request_fields=sorted(str(k)[:60] for k in req)[:40]))
  protocol.diagnostic(conn,'started',req,protocol.SEASON,a)
  conn.commit();return out
 except protocol.MatchError as exc:
  conn.rollback();return protocol.reject(conn,'CMD_START_GAME',req,exc,protocol.SEASON,a)
 except Exception:conn.rollback();raise

def result_key(req):return protocol.receipt_key(protocol.SEASON,protocol.match_id(req))
def handles_result(conn,req):
 try:return protocol.resolve_mode(conn,req)==protocol.SEASON
 except protocol.MatchError:return False
def finish(conn,resp,req):
 begin(conn)
 a=None
 try:
  if protocol.resolve_mode(conn,req)!=protocol.SEASON:
   raise protocol.MatchError('wrong_match_mode')
  gid=protocol.match_id(req)
  cached=protocol.replay(conn,protocol.SEASON,req)
  if cached is not None:conn.commit();return cached
  s=state(conn);a=s.get('active') if s else None
  if not a or not a.get('started'):raise protocol.MatchError('result_before_start')
  if a['fixture']!=s['fixture']:raise protocol.MatchError('stale_fixture')
  resolved=protocol.outcome(req,a)
  h,w=resolved['home'],resolved['away'];result=resolved['result']
  fx,m,_=current(s);matches=fixture_matches(s,fx)
  for match in matches:
   if s['team_id'] in (match['home_team_id'],match['away_team_id']):match.update(home_goals=h,away_goals=w)
  season_rankings.record_fixture(conn,s,a['fixture'],matches)
  s['played'][str(a['fixture'])]=dict(result=result,matches=matches)
  total=len(fixtures(s));s['completed']=a['fixture']==total;s['fixture']=min(total,a['fixture']+1);s['active']=None
  advance_byes(conn,s)
  reward=300 if result=='WIN' else 100 if result=='DRAW' else 0
  store.set_season_points(conn,store.season_points(conn)+reward);save(conn,s)
  out=ziru_data.ml_game_result();out.update(rqid=req.get('rqid'),result='NOERR')
  normal,abnormal=protocol.end_reasons(protocol.SEASON,resolved)
  end=out['game_end_result'];mr=end['match_result'];mr.update(game_res=result,gameend_reason=normal,abnormalend_reason=abnormal,score_data=dict(home_score=h,away_score=w))
  pdata=mr.get('player_data')or{};pdata.update(user_id=a.get('user_id') or req.get('user_id',0),team_id=s['team_id'],side=a['side'],rating=0,oldrating=0,cont_win_num=0)
  if isinstance(pdata.get('user_info'),dict):pdata['user_info'].update(user_id=pdata['user_id'],user_name=store.get_meta(conn,'user_name',''))
  if isinstance(pdata.get('user_info'),dict) and store.get_meta(conn,'avatar',''):
   pdata['user_info']['avatar']=json.loads(store.get_meta(conn,'avatar'))
  mr['player_data']=pdata
  extra=end['extra_match_result'];table=ranking(s);season=extra['season_match_result'];season.update(ranking=table,is_end_season='YES' if s['completed'] else 'NO',is_champion='YES' if s['completed'] and table[0]['team_id']==s['team_id'] else 'NO',is_promote='NO',is_relegate='NO',next_compe_unit_id=s['competition_id'])
  for k in ('league_match_result','event_match_result','gameplayer_update_info','gameplayer_exp_list','get_trial_present','ml_event_match_result','user_compe_core_info'):extra[k]=[]
  extra['campaign_pass_result']=dict(progress_info=dict(count=0),received_random_reward=None,stage_progress_info=dict(current_stage=0),achieved_mission_id_list=[],collection_progress_info=None)
  if 'achieved_mission'in season:season['achieved_mission']=[]
  for k in ('achievement_notification','sheet_list','fame_notification','fameIdList','revealed_bingo_link_card_list','empty_slot_info_list'):end[k]=[]
  if isinstance(end.get('analyst_get_game_result_info'),dict):end['analyst_get_game_result_info'].update(advice_info_match_list=[],analyse_json='[]',advice_json='[]')
  match_stats.record_result(conn,protocol.SEASON,req,a,resolved)
  match_stats.update_response(conn,protocol.SEASON,gid,out)
  protocol.save_receipt(conn,protocol.SEASON,req,out,resolved)
  protocol.diagnostic(conn,'settled',req,protocol.SEASON,a,resolved)
  store.log_event(conn,'my_league_result',dict(game_id=gid,fixture=a['fixture'],score=[h,w],result=result,reward=reward))
  conn.commit();return out
 except (ValueError,TypeError) as exc:
  conn.rollback();return protocol.reject(conn,'CMD_GET_VSCOM_GAME_RESULT',req,exc,protocol.SEASON,a)
 except Exception:conn.rollback();raise
