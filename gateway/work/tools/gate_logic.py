"""Gate response logic: turn a captured response + the local club store into
the reply we send the client (stage 02).

Kept free of mitmproxy imports so it can be unit-tested directly.

Rules:
  - LOGIN            : currency, step-up (tutorial) state, squad list from the store
  - GET_MYCLUB_PLAYER_DATA : the player(s) the client asked for + the ability tables
  - SET_MYCLUB_ENTRY_INFO  : our account, in that command's own entry_info schema
  - GET_MYCLUB_AGENTLIST : agent list from the store
  - USE_MYCLUB_AGENT : record the drawn players + spend, return store currency
  - SET_MYCLUB_SQUAD_INFO / SET_MYCLUB_USER_INFO : persist what the client sent
  - everything else  : captured response, with fresh timestamps
"""
from __future__ import annotations

import json
import copy
import hashlib
import random
import re
import time
import uuid
from pathlib import Path

import club_store as store
import developed_preview
import entry_info
import owned_coaches
import managed_card_pool
import my_league
import myleague_trace
import opponent_squad
import pes_tables
import ziru_data
import account_state
import player_training
import player_operations
import player_release
import ai_division
import local_vscom
import club_pack
import player_skills
import training_settings
import project_info
import announcements
import match_protocol as match
from player_parameters import owned_parameters, BOOSTER_OFFSET
from player_skills import PARAMETER_BY_SKILL

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "work" / "templates"
# Keep the complete manifest captured from a successful current-client login.
# targetAppVersion is a resource compatibility target, not the APK version.
LIVE_DATA_MANIFEST_FILE = TEMPLATES / "CMD_LOGIN_LIVEDATA.json"

# The home banner note line is server text the client renders verbatim, so the
# channel link is served there (the third-party offline package advertises its
# own link through the same field family: banner note_text / agent note_text).
# Single source: club_store, shared with the pack installers and the rewriter.
SOCIAL_LINK = store.SOCIAL_LINK


def norm_group(name) -> str:
    """CAREERPLAN_PARAMTER_SHOOT -> SHOOT (the client spells it "PARAMTER")."""
    n = str(name or "").upper()
    for prefix in ("CAREERPLAN_PARAMTER_", "CAREERPLAN_PARAMETER_", "CAREERPLAN_"):
        if n.startswith(prefix):
            return n[len(prefix):]
    return n


def careerplan_count_from_spent(spent: int) -> int:
    """Convert saved career-plan points into the UI's upgrade count.

    The client displays how many upgrades were bought, while the request and
    the local journal store the total points consumed. Upgrade cost starts at
    one point and increases after every four upgrades (ZIRU's
    ``ziruCountFromSpent`` implementation).
    """
    remaining = max(0, int(spent))
    count = 0
    cost = 1
    while remaining >= cost:
        remaining -= cost
        count += 1
        if count % 4 == 0:
            cost += 1
    return count


def requested_ids(value) -> list[tuple[int, int | None]]:
    """Normalise a `gameplayer_id` request field into (id, serial) pairs.

    The client sends a single {id, serial} object, but the field is a list in
    some commands, so accept both (and a bare id, for hand-written tests).
    """
    out: list[tuple[int, int | None]] = []

    def take(v) -> None:
        if isinstance(v, dict) and "id" in v:
            out.append((int(v["id"]), int(v.get("serial") or 0)))
        elif isinstance(v, (list, tuple)):
            for x in v:
                take(x)
        elif isinstance(v, int):
            out.append((int(v), None))

    take(value)
    return out


def squad_refs(value) -> list[tuple[int, int | None]]:
    """A squad reference list, as `player_list_home` / `player_list_away` send it.

    Each slot is wrapped in a `gameplayer_id` object; hand-written tests
    sometimes pass the bare {id, serial} pair, so both shapes are accepted.
    """
    items = value if isinstance(value, (list, tuple)) else [value]
    unwrapped = [i["gameplayer_id"] if isinstance(i, dict) and "gameplayer_id" in i else i
                 for i in items]
    return requested_ids(unwrapped)


def load_templates() -> dict[str, dict]:
    """Response shapes borrowed from the third-party corpus (commands we never
    captured ourselves), keyed by msgid."""
    out: dict[str, dict] = {}
    if TEMPLATES.is_dir():
        for p in sorted(TEMPLATES.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            mid = d.get("msgid")
            if mid:
                out[mid] = d
    return out


class GateLogic:
    def __init__(self, corpus: dict[str, dict], db: Path | None = None, conn=None):
        self._match_session = uuid.uuid4().hex
        self.responses = corpus
        self.templates = load_templates()
        self.conn = conn if conn is not None else (store.connect(db) if db else store.connect())
        # This process's match state must not inherit an abandoned historical match.
        self.session_match_running = False

    # --- helpers -----------------------------------------------------------
    def _currency(self, resp: dict) -> dict:
        resp["gp"] = int(store.get_meta(self.conn, "gp", "0"))
        ci = dict(resp.get("coin_info") or {})
        ci["coin"] = int(store.get_meta(self.conn, "coin", "0"))
        resp["coin_info"] = ci
        return resp

    @staticmethod
    def _align_live_data_version(resp: dict) -> dict:
        """Replace stale captures with a complete verified resource manifest.

        The revision, compatibility target, hashes and CDN paths form one
        release. Never derive any of them from the APK version or from an
        older release URL. Preserve a newer captured manifest unchanged.
        """
        info = resp.get("livedata_info")
        if not isinstance(info, dict):
            return resp
        verified = json.loads(LIVE_DATA_MANIFEST_FILE.read_text(encoding="utf-8"))
        current = verified["version_file"]
        current = json.loads(current) if isinstance(current, str) else current
        raw = info.get("version_file")
        was_json = isinstance(raw, str)
        try:
            source = json.loads(raw) if was_json else raw
        except (TypeError, ValueError):
            return resp
        if not isinstance(source, dict):
            return resp
        if int(source.get("version", 0)) > int(current["version"]):
            return resp
        replacement = copy.deepcopy(verified)
        if not was_json:
            replacement["version_file"] = copy.deepcopy(current)
        resp["livedata_info"] = replacement
        return resp

    def fixup(self, resp: dict) -> dict:
        now = int(time.time())
        for k in ("page_timestamp", "date", "svr_time"):
            if k in resp:
                resp[k] = now
        steps = resp.get("user_stepup_list")
        if isinstance(steps, list):
            for s in steps:
                if isinstance(s, dict) and "cleared" in s:
                    s["cleared"] = "YES"
        resp.pop("_custom_pack", None)
        return resp

    # --- per-command -------------------------------------------------------
    def handle(self, mid: str, req: dict) -> dict:
        trace_id = myleague_trace.begin_entry(self.conn, mid, req)
        try:
            resp = self._handle(mid, req)
        except Exception as exc:
            myleague_trace.finish_entry(self.conn, trace_id, mid, req, error=exc)
            raise
        myleague_trace.finish_entry(self.conn, trace_id, mid, req, resp)
        return resp

    def _handle(self, mid: str, req: dict) -> dict:
        original_req = req
        # The result adapter resolves only our exact match instances. Recursing
        # through every id/serial here would also rewrite opponent identities.
        if mid != 'CMD_GET_VSCOM_GAME_RESULT':
            req = store.player_identity.canonical_request(self.conn, req)
        if (mid == 'CMD_USE_MYCLUB_ITEM' and req != original_req and any(
                (i.get('item') or {}).get('item_type') == 'LEVEL_TRAINING'
                for i in (req.get('item_list') or []) if isinstance(i, dict))):
            req['_legacy_level_fingerprint'] = hashlib.sha256(json.dumps({k: original_req.get(k) for k in
                ('gameplayer_id_list','item_list','is_auto_use_item')}, sort_keys=True).encode()).hexdigest()
        source = self.responses.get(mid) or self.templates.get(mid)
        resp = copy.deepcopy(source or {"result": "NOERR", "msgid": mid})
        resp["rqid"] = req.get("rqid")
        resp.setdefault("msgid", mid)
        resp = self._align_live_data_version(resp)

        if mid == "CMD_IS_FIRST_LEAGUE" and req.get("is_aileague") == "YES":
            # The local phase is already active; the corpus is a first-visit reply.
            resp.update(is_first_league="NO", result="NOERR")
        elif mid == "CMD_GET_MAINMENU_MATCH_CREATIVE_INFO":
            resp = my_league.root_view(self.conn, resp)
        elif mid == "CMD_GET_LEAGUE_COM_INFO":
            resp = ai_division.info(self.conn, resp)
        elif mid == "CMD_SKIP_LEAGUE_COM_DIVISION":
            resp = ai_division.skip(self.conn, resp, req)
        elif mid == "CMD_GET_VSCOM_GAME_RESULT":
            mode = None
            try:
                mode = match.resolve_mode(self.conn, req)
                if mode == match.SEASON:
                    resp = my_league.finish(self.conn, resp, req)
                elif mode == match.AI:
                    resp = ai_division.finish(self.conn, resp, req)
                elif mode == match.LOCAL:
                    resp = local_vscom.finish(self.conn, resp, req)
            except match.MatchError as exc:
                resp = match.reject(self.conn, mid, req, exc, mode)
        elif mid == "CMD_GET_MYCLUB_PROCURABLE_GAMEPLAYERLIST":
            # Contract "view all" uses this same command with an agent_id.
            aid = int(req.get('agent_id') or 0)
            preview = developed_preview.enabled(self.conn)
            boosts = developed_preview.coach_boosts(self.conn) if preview else {}
            if managed_card_pool.is_managed(self.conn, aid):
                cards = managed_card_pool.preview(self.conn, aid)
                players = [m['gameplayer'] for m in cards['members']] if cards['enabled'] else []
                # the pool shows what each card becomes when fully developed
                # (卡池内展示加点后数据), not its level-1 definition
                if preview:
                    players = [developed_preview.developed(p, boosts) for p in players]
            elif aid:
                # Unknown contract never falls through to the owned/global list.
                players = []
                resp['result'] = 'ERR_INVALID_PARAM'
            else:
                players = store.squad_candidates(self.conn)
            resp['acquired_player_list'] = []
            resp["gameplayer_list"] = [
                {"gameplayer": store.wire_player(p, "list"), "is_dummy": "NO"}
                for p in players
            ]
        elif mid == "CMD_GET_MYCLUB_WAREHOUSE_PLAYERLIST":
            # locker room (储备球员): synthesized shape, same as the player list
            resp["gameplayer_list"] = [
                {"gameplayer": store.wire_player(p, "list"), "is_dummy": "NO"}
                for p in store.warehouse_players(self.conn)
            ]
            resp.setdefault("result", "NOERR")
        elif mid == "CMD_GET_MYCLUB_SCOUT_PLAYERLIST":
            # 普通球员: the signable list. Every card we can serve, flat 73 GP,
            # 100 per page, paged by start_index (protocol from the captured
            # 097_gate_CMD_GET_MYCLUB_SCOUT_PLAYERLIST_resp).
            resp = self._scout_list(resp, req)
        elif mid == "CMD_BUY_MYCLUB_SCOUT_PLAYER":
            resp = self._buy_scout(resp, req)
        elif mid == "CMD_GET_MYCLUB_CONTRACT_INFO":
            resp = club_pack.apply_contract_info(self.conn, resp)
        elif mid == "CMD_GET_MYCLUB_PACK_LIST":
            resp = club_pack.apply_pack_list(self.conn, resp, req)
        elif mid == "CMD_BUY_MYCLUB_PACK":
            resp = club_pack.handle_buy_pack(self.conn, resp, req)
        elif mid == player_release.COMMAND:
            try:
                resp = player_release.release(self.conn, req)
            except player_release.ReleaseError as exc:
                store.log_event(self.conn, 'efootxi_release_rejected',
                                {'reason': str(exc), 'fields': sorted(req)})
                self.conn.commit()
                resp = {'msgid': mid, 'rqid': req.get('rqid'), 'result': 'ERR_INVALID_PARAM'}
        elif mid == "CMD_LOGIN":
            resp = self._currency(resp)
            resp["user_stepup_list"] = store.stepup(self.conn)
            entry = dict(resp.get("entry_info") or {})
            owned_coaches.apply(self.conn, entry)
            # Active and warehouse cards are disjoint account inventories.
            roster = store.entry_roster(self.conn)
            entry["gameplayer_list"] = [store.wire_player(p, "login") for p in roster]
            if roster:
                # the client resolves squad slots against entry_info.gameplayer_list,
                # so both must reference the same (owned) players. Nothing is added
                # to the player objects: an invented field (or a value outside the
                # official range) makes the client's deserializer crash.
                entry["squad_list"] = [
                    store.resolve_squad(self.conn, s, roster) for s in store.squads(self.conn)
                ]
            elif store.squads(self.conn):
                entry["squad_list"] = store.squads(self.conn)
            entry["tutorial_end"] = "YES"
            entry["main_squad_id"] = store.main_squad_id(self.conn)
            entry["squad_id"] = entry["main_squad_id"]
            # warehouse / locker room: the client disables the entry when the
            # capacity is 0 (E04), so serve a real capacity from the store
            store.apply_player_capacity(self.conn, entry)
            store.apply_uniform_selection(self.conn, entry)
            resp["entry_info"] = entry
        elif mid in ("CMD_GET_MYCLUB_AGENTLIST", "CMD_USE_MYCLUB_AGENT"):
            agents = store.agents(self.conn)
            active = store.get_meta(self.conn, "league_position_agent_ids", "")
            if active:
                active_ids = json.loads(active)
                by_id = {a.get('agent_id'): a for a in agents}
                agents = [by_id[aid] for aid in active_ids if aid in by_id]
            if agents:
                resp["agent_list"] = managed_card_pool.decorate_agents(self.conn, agents)
                # the captured agents still carry the ORIGINAL account's pool
                # counts and pickup cards; when our draw pool is empty the client
                # must not show those, so every count/featured list is zeroed.
                if store.pool_size(self.conn) == 0:
                    for a in resp["agent_list"]:
                        if managed_card_pool.is_managed(self.conn, int(a.get("agent_id") or 0)):
                            continue
                        a["player_list_total"] = 0
                        a["remaining_player_count"] = [0, 0, 0, 0, 0]
                        a["base_player_count"] = [0, 0, 0, 0, 0]
                        a["pickup_list"] = []
                        a["banner_a_pickup_list"] = []
                        a["banner_c_pickup_list"] = []
                        a["pickup_cardtype_list"] = []
                        a["acquired_list"] = []
                prices = store.get_meta(self.conn, "agent_price_list", "")
                if prices:
                    try:
                        resp["agent_price_list"] = json.loads(prices)
                    except (TypeError, ValueError):
                        pass
            if mid == "CMD_USE_MYCLUB_AGENT":
                resp = self._use_agent(resp, req)
        elif mid == "CMD_GET_MYCLUB_AGENT_BOX_INFO":
            aid = int(req.get("agent_id") or 0)
            if managed_card_pool.is_managed(self.conn, aid):
                b = managed_card_pool.counts(self.conn, aid)
                resp["remaining_player_count"] = b
                resp["base_player_count"] = managed_card_pool.counts(self.conn, aid, remaining=False)
                resp["can_reset"] = "YES" if managed_card_pool.can_reset(self.conn, aid) else "NO"
        elif mid == "CMD_RESET_MYCLUB_AGENT":
            try:
                raw_id = req.get("agent_id")
                if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
                    raise ValueError("invalid agent id")
                aid = int(raw_id)
                managed_card_pool.reset_agent(self.conn, aid, request=req)
                resp["result"] = "NOERR"
            except (TypeError, ValueError):
                resp["result"] = "ERR_INVALID_PARAM"
        elif mid == "CMD_GET_MYCLUB_PLAYER_DATA":
            resp = self._player_data(resp, req)
        elif mid == "CMD_USE_MYCLUB_ITEM":
            kinds = [(i.get("item") or {}).get("item_type") for i in (req.get("item_list") or []) if isinstance(i, dict)]
            if "LEVEL_TRAINING" in kinds:
                resp = self._level_training(resp, req)
            elif any(k in ('POSITION_TRAINING', 'POSITION_TRAINING_SELECT') for k in kinds):
                resp = self._position_training(resp, req)
            elif any(k in ('SKILL_TRAINING', 'SKILL_TRAINING_SELECT', 'SKILL_TRAINING_CATEGORY') for k in kinds):
                resp = self._skill_training(resp, req)
            else:
                resp = self._booster_training(resp, req)
        elif mid == "CMD_DELETE_MYCLUB_SKILL":
            resp = {'msgid': mid, 'rqid': req.get('rqid'), 'result': 'NOERR'}
            try:
                resp.update(player_operations.transact(self.conn, 'delete_skill', req,
                            lambda: player_operations.delete_skill(self.conn, req)))
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                resp['result'] = 'ERR_INVALID_PARAM'
                store.log_event(self.conn, 'skill_delete_rejected', {'reason': str(exc)})
                self.conn.commit()
        elif mid in ("CMD_GET_MYCLUB_ENTRY_INFO", "CMD_GET_ENTRY_INFO"):
            resp = self._entry_snapshot(resp, req)
        elif mid == "CMD_SET_MYCLUB_ENTRY_INFO":
            try:
                resp = self._set_entry_info(resp, req)
            except ValueError:
                return {'msgid':mid,'rqid':req.get('rqid'),'result':'ERR_INVALID_PARAM'}
        elif mid == "CMD_SET_MYCLUB_BRING_UNIFORM":
            resp = self._set_bring_uniform(resp, req)
        elif mid == "CMD_CREATE_MYCLUB_SQUAD":
            try:
                elem = store.create_squad(self.conn, req)
                resp["new_squad_data"] = store.resolve_squad(
                    self.conn, elem, store.entry_roster(self.conn)
                )
                resp["result"] = "NOERR"
                store.log_event(self.conn, "squad_create", {"squad_id": elem.get("squad_id")})
                self.conn.commit()
            except ValueError:
                resp["result"] = "ERR_INVALID_PARAM"
        elif mid == "CMD_DELETE_MYCLUB_SQUAD":
            sid = req.get("squad_id") or req.get("main_squad_id")
            try:
                if sid is None:
                    raise ValueError("missing squad id")
                active = store.delete_squad(self.conn, int(sid))
                resp["main_squad_id"] = active
                resp["result"] = "NOERR"
                store.log_event(self.conn, "squad_delete", {"squad_id": int(sid), "main_squad_id": active})
                self.conn.commit()
            except (TypeError, ValueError):
                resp["result"] = "ERR_INVALID_PARAM"
        elif mid == "CMD_SET_MYCLUB_SQUAD_INFO":
            # the client sends `squad_id` + `squad_data` at the top level; reading
            # squad_info/squad here meant every lineup edit was dropped (E13)
            try:
                elem = store.save_squad_info(self.conn, req)
            except ValueError:
                return {'msgid': mid, 'rqid': req.get('rqid'), 'result': 'ERR_INVALID_PARAM'}
            data = elem.get("squad_data") or {}
            store.log_event(self.conn, "squad_save", {
                "squad_id": elem.get("squad_id"),
                "slots": len(data.get("player_list") or []),
                "team_power": (req.get("squad_log") or {}).get("team_power"),
            })
            self.conn.commit()
        elif mid == "CMD_SET_MYCLUB_MAIN_SQUAD":
            sid = req.get("squad_id") or req.get("main_squad_id")
            try:
                if sid is None:
                    raise ValueError("missing squad id")
                store.set_main_squad(self.conn, int(sid))
                store.log_event(self.conn, "main_squad", {"squad_id": int(sid)})
                self.conn.commit()
            except (TypeError, ValueError):
                resp["result"] = "ERR_INVALID_PARAM"
        elif mid == "CMD_SET_MYCLUB_SQUAD_NAME":
            sid = req.get("squad_id") or store.main_squad_id(self.conn)
            name = req.get("squad_name") or req.get("name")
            try:
                if not isinstance(name, str) or not name:
                    raise ValueError("missing squad name")
                store.set_squad_name(self.conn, int(sid), name)
                store.log_event(self.conn, "squad_name", {"squad_id": int(sid), "name": name})
                self.conn.commit()
            except (TypeError, ValueError):
                resp["result"] = "ERR_INVALID_PARAM"
        elif mid == "CMD_SET_MYCLUB_CAREERPLAN_ALLOCATE_TYPE":
            resp = training_settings.handle(self.conn, req)
        elif mid == "CMD_USE_MYCLUB_CAREERPLAN":
            resp = self._careerplan(resp, req)
        elif mid == "CMD_SET_AVATAR_INFO":
            resp = account_state.save_avatar(self.conn, req, resp)
        elif mid == "CMD_SET_MYCLUB_USER_INFO":
            for key in ("team_name", "team_name_short", "user_name"):
                if key in req:
                    store.set_meta(self.conn, key, str(req[key]))
            store.log_event(self.conn, "user_info", {k: req.get(k) for k in ("team_name", "user_name")})
            self.conn.commit()
        # --- ported gameplay surface (TASKS R18) ---------------------------
        elif mid in ("CMD_GET_VSCOM_OPPONENT", "CMD_GET_MYCLUB_VSCOM_OPPONENT"):
            resp = self._vscom_opponent(mid, resp, req)
        elif mid == "CMD_START_GAME":
            mode = None
            try:
                mode = match.resolve_mode(self.conn, req, include_receipts=False)
                if mode == match.SEASON:
                    refs = my_league.start_refs(self.conn, req)
                    resp = my_league.start(self.conn, resp, req, self._home_entries(refs, log_substitutes=False))
                else:
                    resp = self._start_game(resp, req)
            except (match.MatchError, ValueError) as exc:
                resp = match.reject(self.conn, mid, req, exc, mode)
        elif mid == "CMD_SET_SEASON_ENTER":
            resp = my_league.select(self.conn, resp, req)
        elif mid == "CMD_SET_MYCLUB_SPECIAL_DREAM_SQUAD_INFO":
            resp = my_league.set_special(self.conn, resp, req)
        elif mid == "CMD_GET_SEASON_SCHEDULE":
            resp = my_league.get_schedule(self.conn, resp, req) if my_league.state(self.conn) else self._season_schedule(resp, req)
        elif mid == "CMD_GET_SEASON_INFO":
            resp = my_league.info(self.conn, resp, req) if my_league.state(self.conn) else self._season_info(resp, req)
        elif mid == "CMD_RECV_CAMPAIGN_PASS_REWARD":
            resp = self._campaign_pass(resp, req)
        elif mid == "CMD_EXCHANGE_SEASON_POINT_ITEM":
            resp = self._exchange_item(resp, req)
        elif mid == "CMD_EXCHANGE_SEASON_POINT_RENTALPLAYER":
            resp = self._exchange_rental(resp, req)
        elif mid in ("CMD_GET_SEASON_POINT_RENTALPLAYER_LIST", "CMD_GET_SEASON_POINT_ITEM_LIST"):
            resp = self._season_point_list(mid, resp, req)

        # Activity payloads contain nested reward/ad objects. Rewrite only
        # actual URL/Telegram strings so captured package links are replaced
        # without touching player data or resource paths.
        if mid in ('CMD_GET_EVENT_LIST_AND_LOG', 'CMD_GET_EVENT_LIST_AND_INFO',
                   'CMD_GET_ONLINE_AD_FILE', 'CMD_GET_ONLINE_AD_FILE_LIST',
                   'CMD_GET_ONLINE_MATCH_INFO'):
            resp = self._rewrite_activity_links(resp)

        # any response carrying currency is served from the store
        # (LOGIN, GET_MAINMENU_INFO, USE_MYCLUB_AGENT, ...)
        if "gp" in resp or "coin_info" in resp:
            resp = self._currency(resp)

        player_training.lock_reply_inventory(resp, mid)
        account_state.apply(self.conn, resp, mid, req)
        training_settings.apply(self.conn, resp)
        resp = self.fixup(self._quiet_home(mid, resp))
        if resp.get('result') == 'NOERR':
            if mid == 'CMD_START_GAME':
                self.session_match_running = True
            elif mid == 'CMD_GET_VSCOM_GAME_RESULT':
                self.session_match_running = False
        resp = announcements.apply(self.conn, mid, req, resp)
        return resp

    @staticmethod
    def _rewrite_activity_links(value):
        """Keep image/CDN URLs intact; replace only inherited promotional text."""
        return project_info.replace_legacy_link(value)

    @staticmethod
    def _quiet_home(mid: str, resp: dict) -> dict:
        """Keep home posters, but silence popup promotions and unread badges."""
        changes = {}
        if mid == 'CMD_LOGIN':
            # The recommendation consent is read from LOGIN before the home
            # response. ON is the persisted "already answered" value.
            if 'banner_control_setting' in resp:
                resp['banner_control_setting'] = 'ON'
            # The earliest login capture is a NEW_USER fixture. Do not replay
            # that first-visit home guidance for this existing local account.
            if 'menu_trackrecord_user_type' in resp:
                resp['menu_trackrecord_user_type'] = 'NONE'
            changes.update(fame_notification_list=[],
                           # Clear the daily-game coach-mark before the first
                           # MAINMENU response is processed by the client.
                           is_sugoroku='NO',
                           go_around_present_hide='YES',
                           go_around_present_list=[],
                           go_around_loop=0,
                           current_go_around_num=0)
            # LOGIN responses from older captures omit these keys. Add the
            # explicit cleared values so the client cannot reuse stale state.
            resp.update(is_sugoroku='NO', go_around_present_hide='YES',
                        go_around_present_list=[], go_around_loop=0,
                        current_go_around_num=0)
        elif mid in ('CMD_GET_MAINMENU_INFO', 'CMD_GET_MYCLUB_MAINMENU_INFO'):
            # The carousel and automatic popup share the same banner list.
            # Preserve the posters and their navigation data; only mute popups,
            # and put the channel link on the note line under each banner.
            for banner in resp.get('banner_list') or []:
                if not isinstance(banner, dict):
                    continue
                if 'is_popup' in banner:
                    banner['is_popup'] = 'NO'
                banner['note_text'] = SOCIAL_LINK
            changes.update({k: [] for k in (
                'agent_list', 'agent_price_list', 'agent_bonus_list',
                'season_movie_info', 'login_bonus_list', 'added_present_list',
                'added_coin_list', 'achievement_notification', 'fame_notification_list',
                'event_guide_alert_info', 'new_bingo_link_card', 'home_campain_item_list')})
            changes.update({k: 0 for k in (
                'present_num', 'my_contract_num', 'unread_information_count',
                'unread_promotion_num', 'new_arrival_friend_request_num',
                'path_to_glory_num_of_achievements', 'dailybonus_guide',
                'shop_advertising_remaining_time')})
            # The daily-game coach-mark is driven by the home response itself
            # when returning from another screen. Clear every related field so
            # it cannot be recreated on the next home refresh.
            changes.update({
                'added_present_list': [],
                'go_around_present_list': [],
                'go_around_present_hide': 'YES',
                'go_around_loop': 0,
            })
            changes.update({k: 'NO' for k in (
                'is_news_popup', 'is_sugoroku', 'is_coin_sale', 'is_starter_pack',
                'is_void', 'is_gp_shop_campaign', 'is_exchangeable_free_coin',
                'is_exist_request_to_join_room', 'is_need_ranking_changed_alert')})
            # The client shows the "recommended horizontal banners" consent
            # dialog while this remains UNSELECTED.  Treat the preference as
            # already answered and keep it stable on every home refresh.
            resp['banner_control_setting'] = 'ON'
            changes.update({k: '' for k in (
                'coin_banner_file_name', 'point_banner_file_name',
                'gp_shop_banner_file_name', 'comeback_achievement_banner_d_file_name')})
            recruit = resp.get('recruit_info_for_mainmenu')
            if isinstance(recruit, dict):
                for key in ('is_active', 'is_new', 'can_input_code'):
                    if key in recruit:
                        recruit[key] = 'NO'
            # These fields are absent from some captured home responses, but
            # the client still reads them when it rebuilds the coach-mark.
            # Write the cleared state into every home reply, including the
            # return-to-home response after another screen.
            resp['added_present_list'] = []
            resp['go_around_present_list'] = []
            resp['go_around_present_hide'] = 'YES'
            resp['go_around_loop'] = 0
            resp['current_go_around_num'] = 0
            resp['is_sugoroku'] = 'NO'
        elif mid == 'CMD_GET_MYCLUB_CONTRACT_INFO':
            if not resp.get('_custom_pack'):
                changes.update(playerpack_num=0, banner_playerpack_b_name='')
        elif mid == 'CMD_GET_MYCLUB_PACK_LIST':
            if not resp.get('_custom_pack'):
                changes.update(pack_list=[], note_text='')
        elif mid == 'CMD_GET_MYCLUB_PRESENTLIST':
            changes.update(present_list=[], present_list_num=0)
        elif mid == 'CMD_GET_MYCLUB_NEWSLIST':
            changes.update(news_list=[], news_list_num=0)
        elif mid == 'CMD_GET_SUGOROKU':
            # The daily mini-game response carries the "win a legendary
            # player" coach-mark.  Hide the reward/prompt state while keeping
            # the endpoint valid for clients that still request it.
            changes.update(is_sugoroku='NO', go_around_present_hide='YES',
                           go_around_present_list=[], current_go_around_num=0)
        for key, value in changes.items():
            if key in resp:
                resp[key] = value
        if mid in ('CMD_GET_MAINMENU_INFO', 'CMD_GET_MAINMENU_MATCH_CREATIVE_INFO'):
            # Keep the task/match menus usable while removing unread counters.
            for key in ('achievement_list', 'sheet_list'):
                for item in resp.get(key) or []:
                    if isinstance(item, dict) and 'not_received_num' in item:
                        item['not_received_num'] = 0
        return resp

    # --- entry info (选球队 / 点 START) ------------------------------------
    def _entry_snapshot(self, resp: dict, req: dict) -> dict:
        """Serve the current owned roster after leaving a player screen.

        The captured GET_ENTRY_INFO reply belongs to a different account.  It
        is especially harmful here because the client replaces its lineup
        cache with this response and then drops any card absent from the list.
        Reuse its schema as a template, but build the roster and squad refs
        from the local store.
        """
        template = resp.get("entry_info")
        if not isinstance(template, dict):
            for candidate in (self.responses.get("CMD_GET_MYCLUB_ENTRY_INFO"),
                              self.responses.get("CMD_GET_ENTRY_INFO")):
                if isinstance(candidate, dict) and isinstance(candidate.get("entry_info"), dict):
                    template = candidate["entry_info"]
                    break
        if not isinstance(template, dict):
            template = {"gameplayer_list": [], "squad_list": []}
        resp["entry_info"] = entry_info.build_snapshot(self.conn, template)
        owned_coaches.apply(self.conn, resp["entry_info"])
        resp["result"] = "NOERR"
        resp["errcode"] = "NOERR"
        store.log_event(self.conn, "entry_info_get", {
            "players": len(resp["entry_info"].get("gameplayer_list") or []),
            "squads": len(resp["entry_info"].get("squad_list") or []),
        })
        self.conn.commit()
        return resp

    # --- 普通球员 (scout / signable list) ----------------------------------
    SCOUT_GP = 73
    SCOUT_PAGE = 100

    @classmethod
    def _scout_price(cls) -> dict:
        return {"can_use_coin": "NO", "coin_price_base": 0, "coin_price": 0,
                "can_use_gp": "YES", "gp_price_base": cls.SCOUT_GP,
                "gp_price": cls.SCOUT_GP}

    def _scout_list(self, resp: dict, req: dict) -> dict:
        """The signable list = the project's whole card library, not the owned set.

        The list used to be `store.players()`, so it only ever showed the cards
        the account already owned - after the inventory was pruned to the active
        squad that is 25 rows out of the 7094-card library. Every registered card
        is signable for 73 GP, so the library is the right source (and the same
        reason a purchase resolves against it, see _buy_scout).
        """
        info = req.get("search_info") or {}
        cards = self._scout_sort(self._scout_filter(managed_card_pool.library(self.conn), info), info)
        start = max(0, int(req.get("start_index") or 0))
        page = cards[start:start + self.SCOUT_PAGE]
        preview = developed_preview.enabled(self.conn)
        boosts = developed_preview.coach_boosts(self.conn) if preview else {}
        rows = []
        for p in page:
            gid = p.get("gameplayer_id") or {}
            p = dict(p, gameplayer_id={"id": int(gid.get("id") or 0), "serial": 0})
            if preview:
                p = developed_preview.developed(p, boosts)
            rows.append({"playerinfo": store.wire_player(p, "login"), "price": self._scout_price()})
        resp["gameplayer_list"] = rows
        resp["gameplayer_list_total"] = len(cards)
        resp["result"] = "NOERR"
        return resp

    # the client's 筛选 payload (captured 2026-10-15 from a live request):
    #   {'sort_type': 'PRICE', 'ascending_order': 'NO',
    #    'narrow_down_type': ['NONE','NATION','LEAGUE'],
    #    'nation_id': [204], 'league_id': [549], 'team_id': [], 'position': [],
    #    'height': [], 'age': [], 'rarity': [], 'price': [], 'overall': [],
    #    'condition': [], 'skill': [], 'playstyle': []}
    # Filter values come straight from the client's live requests:
    #   position  ['GK']                       -> position NAMES, not codes
    #   skill     ['SKILL_CARD_DOUBLE_TOUCH']  -> SKILL_CARD_* enum names
    #   team_id / nation_id / league_id        -> id lists (eFootHub id space)
    #   age / height / overall / price         -> [min, max] pairs
    # narrow_down_type lists which categories are actually active.
    POSITION_NAMES = ["GK", "CB", "LB", "RB", "DMF", "CMF", "LMF", "RMF", "AMF", "LWF", "RWF", "SS", "CF"]
    SKILL_ALIAS = {"heading": "headingSkill", "aerial_fort": "aerialForte",
                   "won_ballon_dor": "wonBallonDor", "phenomenal_pass": "phenomenalPassing"}

    @classmethod
    def _skill_name(cls, raw) -> str:
        s = str(raw)
        if s.upper().startswith("SKILL_CARD_"):
            s = s[len("SKILL_CARD_"):]
        s = s.lower()
        return cls.SKILL_ALIAS.get(s, "".join(
            w if i == 0 else w.capitalize() for i, w in enumerate(s.split("_"))))

    @classmethod
    def _scout_filter(cls, cards: list[dict], info: dict) -> list[dict]:
        def ids(key):
            return [v for v in (info.get(key) or []) if isinstance(v, int)]

        def rng(key):
            v = ids(key)
            return (v[0], v[-1]) if len(v) >= 2 else (v[0], v[0]) if v else None

        nation, league, team = ids("nation_id"), ids("league_id"), ids("team_id")
        rarity = ids("rarity")
        positions = {cls.POSITION_NAMES.index(p) for p in (info.get("position") or [])
                     if p in cls.POSITION_NAMES}
        skills = {cls._skill_name(s) for s in (info.get("skill") or [])}
        age, height, overall = rng("age"), rng("height"), rng("overall")
        if not any((nation, league, team, rarity, positions, skills, age, height, overall)):
            return cards
        out = []
        for p in cards:
            t = p.get("parameters") or []
            get = lambda i: t[i] if len(t) > i else 0
            if nation and p.get("_nation_id") not in nation:
                continue
            if league and p.get("_league_id") not in league:
                continue
            if team and p.get("initial_base_team_id") not in team:
                continue
            if positions and get(0) not in positions:
                continue
            if rarity and p.get("default_rarity") not in rarity:
                continue
            if skills and not skills <= set(p.get("_skills") or []):
                continue
            if age and not age[0] <= get(46) <= age[1]:
                continue
            if height and not height[0] <= get(47) <= height[1]:
                continue
            if overall and not overall[0] <= (p.get("_overall") or 0) <= overall[1]:
                continue
            out.append(p)
        return out

    @staticmethod
    def _scout_sort(cards: list[dict], info: dict) -> list[dict]:
        key = str(info.get("sort_type") or "").upper()
        if key not in ("PRICE", "RARITY", "POSITION", "AGE", "HEIGHT", "OVERALL"):
            return cards

        def val(p):
            t = p.get("parameters") or []
            if key == "OVERALL":
                return p.get("_overall") or 0
            if key in ("PRICE", "RARITY"):
                return p.get("default_rarity") or 0
            i = {"POSITION": 0, "AGE": 46, "HEIGHT": 47}[key]
            return t[i] if len(t) > i else 0

        return sorted(cards, key=val, reverse=(str(info.get("ascending_order") or "NO").upper() != "YES"))

    @staticmethod
    def _target_id(target) -> int:
        """The buy request's shape is not fixed (playerinfo wrapper, bare
        gameplayer_id, nested lists); walk whatever the client sent for the first
        non-zero gameplayer_id.id. Reading only one shape meant every purchase
        resolved to the same card."""
        def walk(o):
            if isinstance(o, dict):
                if type(o.get('id')) is int and o['id'] > 0:
                    return o['id']
                gid = o.get("gameplayer_id")
                if isinstance(gid, dict) and isinstance(gid.get("id"), int) and gid["id"]:
                    return gid["id"]
                for v in o.values():
                    r = walk(v)
                    if r:
                        return r
            elif isinstance(o, list):
                for v in o:
                    r = walk(v)
                    if r:
                        return r
            return None

        return int(walk(target) or 0)

    def _buy_scout(self, resp: dict, req: dict) -> dict:
        pid = self._target_id(req.get("target_player"))
        # A rejected purchase must never return the captured account's player.
        resp.pop('gameplayer', None)
        self.conn.execute('CREATE TABLE IF NOT EXISTS scout_purchase_receipts('
                          'user_id TEXT NOT NULL,rqid TEXT NOT NULL,card_id INTEGER NOT NULL,'
                          'player TEXT NOT NULL,PRIMARY KEY(user_id,rqid))')
        key = (str(req.get('user_id', 0)), str(req.get('rqid')))
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            if req.get('rqid') is not None:
                receipt = self.conn.execute('SELECT card_id,player FROM scout_purchase_receipts '
                                            'WHERE user_id=? AND rqid=?', key).fetchone()
                if receipt:
                    if receipt[0] != pid:
                        raise ValueError('ERR_INVALID_PARAM')
                    self.conn.commit()
                    receipt_player = json.loads(receipt[1])
                    receipt_gid = receipt_player.get('gameplayer_id') or {}
                    saved = store.player_by_key(self.conn, int(receipt_gid.get('id', 0)), int(receipt_gid.get('serial', 0)))
                    if saved is None:
                        raise ValueError('ERR_INVALID_PARAM')
                    sgid = saved.get('gameplayer_id') or {}
                    reserved = store.in_warehouse(self.conn, int(sgid.get('id', 0)),
                                                  int(sgid.get('serial', 0)))
                    resp.update(gameplayer={'gameplayer': store.wire_player(saved, 'login'),
                                            'is_warehouse': 'YES' if reserved else 'NO'},
                                result='NOERR', bought='YES')
                    resp.update(gp=int(store.get_meta(self.conn, 'gp', '0')), sound_type=0,
                                achievement_notification=[], warehouse_num=store.warehouse_count(self.conn))
                    return resp
            card = managed_card_pool.library_entry(self.conn, pid) if pid else None
            if card is None:
                # A card id the library does not know can still be an owned
                # instance (captured imports, rentals): never refuse those.
                card = store.player_by_key(self.conn, pid) if pid else None
            if card is None:
                raise ValueError('ERR_INVALID_PARAM')
            if int(store.get_meta(self.conn, 'gp', '0')) < self.SCOUT_GP:
                raise ValueError('ERR_INSUFFICIENT_GP')
            # Never move or overwrite an existing owned copy of the selected card.
            card['gameplayer_id'] = {'id': pid, 'serial': store.player_identity.allocate(self.conn)}
            card['acquisition_date'] = int(time.time())
            for field in ('games', 'goal', 'assist', 'red_card'):
                card[field] = 0
            card['is_lock'] = 'NO'
            store.spend(self.conn, gp=self.SCOUT_GP)
            if not store.add_player(self.conn, card, f"scout:{pid}"):
                raise ValueError('ERR_INVALID_PARAM')
            # The captured official reply (corpus 057) keeps a bought 普通球员 in
            # the active list: is_warehouse NO. Only a full active list routes the
            # card to the reserve slots, which is exactly the client's
            # "球员槽不足。移至储备球员槽。" message.
            gid = card["gameplayer_id"]
            to_warehouse = store.active_count(self.conn) > store.PLAYER_CAPACITY
            if to_warehouse:
                store.move_to_warehouse(self.conn, int(gid["id"]), int(gid.get("serial", 0)))
            card = store.player_by_key(self.conn, pid, gid['serial'])
            store.log_event(self.conn, "scout_buy",
                            {"id": pid, "cost": self.SCOUT_GP, "to_warehouse": to_warehouse})
            if req.get('rqid') is not None:
                self.conn.execute('INSERT INTO scout_purchase_receipts VALUES(?,?,?,?)',
                                  (*key, pid, json.dumps(card)))
            self.conn.commit()
            resp["gameplayer"] = {"gameplayer": store.wire_player(card, "login"),
                                  "is_warehouse": "YES" if to_warehouse else "NO"}
            resp.update(result='NOERR', bought='YES')
        except ValueError as exc:
            self.conn.rollback()
            resp.update(result=str(exc) if str(exc).startswith('ERR_') else 'ERR_INVALID_PARAM', bought='NO')
        except Exception:
            self.conn.rollback()
            raise
        resp["gp"] = int(store.get_meta(self.conn, "gp", "0"))
        resp["sound_type"] = 0
        resp["achievement_notification"] = []
        resp["warehouse_num"] = store.warehouse_count(self.conn)
        return resp

    def _set_entry_info(self, resp: dict, req: dict) -> dict:
        """Answer with OUR account, in THIS command's schema.

        This is the client telling the server which base team / squad the user
        picked (the team-select screen's START). There was no handler, so it
        replayed a capture taken from a different account: measured 2026-10-14
        23:11 the client submitted `base_team_id: 0, team_name: "-"` and got back
        `base_team_id: 108, team_name: "FC Barcelona", user_name: "Offline
        Barca"` plus that account's 28-player roster - and crashed rebuilding its
        state from a stranger's account, which is exactly where the log stops.

        The three entry_info schemas the game uses are not interchangeable
        (see work/tools/entry_info.py), so the captured reply is kept as a shape
        template and only the fields we own are overwritten.
        """
        template = resp.get("entry_info")
        if not isinstance(template, dict):
            template = {}

        # the squad the client just submitted is the squad it will render next
        if isinstance(req.get("squad_data"), dict) and req["squad_data"]:
            store.save_squad_info(self.conn, req)
        base_team = req.get("base_team")
        if isinstance(base_team, dict):
            try:
                old_team = store.base_team_id_of(json.loads(store.get_meta(self.conn, "base_team", "{}") or "{}"))
            except ValueError:
                old_team = 0
            store.set_meta(self.conn, "base_team", json.dumps(base_team, ensure_ascii=False))
            # a new base team wears its own kits until the player picks again
            store.retarget_uniforms(self.conn, old_team, store.base_team_id_of(base_team))

        entry = entry_info.build(self.conn, req, template)
        owned_coaches.apply(self.conn, entry)
        store.log_event(self.conn, "entry_info_set", entry_info.summary(entry))
        self.conn.commit()
        resp["entry_info"] = entry
        resp["result"] = "NOERR"
        resp["errcode"] = "NOERR"
        return resp

    def _set_bring_uniform(self, resp: dict, req: dict) -> dict:
        """CMD_SET_MYCLUB_BRING_UNIFORM: remember the kits the player picked.

        The client sends `set_uniform_list` with kit ids (base_team_id * 16384 +
        slot, see club_store.UNIFORM_STRIDE). This command had no handler, so the
        selection was acknowledged and dropped - matches kept wearing the captured
        club's kits (user report, 2026-10-15). The reply mirrors the reference
        offline package's ack for the team commands (z27TeamChange): the base
        team, the fresh entry_info and the main squad id.
        """
        ids = req.get("set_uniform_list")
        if not isinstance(ids, list):
            candidate = req.get("uniform_list")
            if isinstance(candidate, dict):
                candidate = candidate.get("uniform_list")
            ids = candidate if isinstance(candidate, list) else None
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 8
                or any(type(i) is not int or i <= 0 for i in ids)):
            store.log_event(self.conn, "bring_uniform_rejected", {"ids": str(ids)[:120]})
            self.conn.commit()
            resp["result"] = "ERR_INVALID_PARAM"
            return resp
        store.set_uniforms(self.conn, ids)
        template = self.responses.get("CMD_SET_MYCLUB_ENTRY_INFO") or {}
        entry = entry_info.build(self.conn, req, template.get("entry_info") or {})
        resp["entry_info"] = entry
        want = store.main_squad_id(self.conn)
        squads = store.squads(self.conn)
        squad = next((s for s in squads if int(s.get("squad_id") or 0) == want), None) or (squads[0] if squads else {})
        if isinstance(squad.get("base_team"), list) and squad.get("base_team"):
            resp["base_team"] = squad["base_team"]
        elif isinstance(req.get("base_team"), dict):
            resp["base_team"] = [req["base_team"]]
        resp["main_squad_id"] = int((squad or {}).get("squad_id") or want or 1)
        resp["result"] = "NOERR"
        resp["errcode"] = "NOERR"
        store.log_event(self.conn, "bring_uniform", {"ids": ids, "base_team": resp.get("base_team")})
        self.conn.commit()
        return resp

    # --- player detail ----------------------------------------------------
    def _player_data(self, resp: dict, req: dict) -> dict:
        """Serve the player(s) the client asked for, out of the store.

        The handler used to replay a captured response taken from a different
        account, so `gameplayer_list` never contained the requested id. The
        player detail / ability-point screens look the requested player up in
        that list: with nothing to find the screen came up empty (no allocate
        buttons, E14) and the client dropped the player when it left (E15).

        The reply also has to carry the tables the ability-point screen is built
        from - `careerplan_list` is the list of allocatable parameter groups, so
        without it the screen has no rows and therefore no "+" buttons (E14).
        The field set comes from the third-party offline package, which ships
        the same command's response (ZIRU27_ANALYSIS §6).
        """
        wanted = requested_ids(req.get("gameplayer_id"))
        found: list[dict] = []
        source_player = None
        for gid, serial in wanted:
            p = store.player_by_key(self.conn, gid, serial)
            if p is None and not store.player_identity.enabled(self.conn):
                p = store.player_by_key(self.conn, gid, None)   # serial can differ
            if p is None:
                store.log_event(self.conn, "player_data_miss", {"id": gid, "serial": serial})
                continue
            player_training.ensure_progression_ledger(self.conn, p)
            found.append(store.wire_player(p, "detail"))
            if source_player is None:
                source_player = p
        resp["gameplayer_list"] = found
        resp["careerplan_list"] = self._careerplan_list(found)
        resp["original_parameters"] = list((source_player or {}).get('_careerplan_base_parameters')
                                            or (found[0].get('parameters') if found else []) or [])
        resp["experience_list"] = player_training.experience_table(source_player) if source_player else []
        resp["skillcard_id_list"] = player_skills.selectable_skill_ids(source_player)
        resp["booster_training_info"] = json.loads(json.dumps(pes_tables.BOOSTER_TRAINING_INFO))
        resp["result"] = "NOERR"
        resp["errcode"] = "0"
        store.log_event(self.conn, "player_data", {"asked": wanted, "served": len(found)})
        self.conn.commit()
        return resp

    def _careerplan_list(self, players: list[dict]) -> list[dict]:
        """The allocation groups, taken from the LOGIN corpus verbatim.

        `common_careerplan_info` in the captured LOGIN reply is exactly this
        structure (type / effect / data_list of parameter_type groups), so the
        definition stays single-sourced instead of copied into code. The journal
        stores consumed points; `count` is the number of upgrades bought and
        `point` is the cost of the next upgrade (one extra point every four).
        """
        info = None
        for mid in ("CMD_LOGIN", "CMD_GET_MYCLUB_ENTRY_INFO"):
            src = self.responses.get(mid) or self.templates.get(mid)
            if isinstance(src, dict):
                info = src.get("common_careerplan_info")
                if not info:
                    e = src.get("entry_info")
                    if isinstance(e, dict):
                        info = e.get("careerplan_info")
                if info:
                    break
        if not isinstance(info, dict):
            return []
        out = json.loads(json.dumps(info, ensure_ascii=False))
        if players:
            pid = players[0].get("gameplayer_id") or {}
            counts = {norm_group(k): careerplan_count_from_spent(v)
                     for k, v in store.careerplan_counts(self.conn, int(pid.get("id", 0)), int(pid.get('serial',0))).items()}
            for group in out.get("data_list") or []:
                if isinstance(group, dict):
                    key = norm_group(group.get("parameter_type"))
                    group.setdefault("parameter_list", list(pes_tables.CAREERPLAN_PARAM_LIST.get(key, [])))
                    group["count"] = counts.get(key, 0)
                    group["point"] = group["count"] // 4 + 1
        return [out]

    def _level_training(self, resp: dict, req: dict) -> dict:
        """Commit XP/levels/points and return the native training update shape."""
        for key in ('skill_training_result', 'ability_training_result', 'position_training_result', 'experience'):
            resp.pop(key, None)
        resp['gameplayer_list'] = []
        resp['gameplayer_exp_list'] = []
        try:
            wanted = requested_ids(req.get('gameplayer_id_list'))
            if len(wanted) != 1 or wanted[0][1] is None:
                raise ValueError('expected one exact player instance')
            gid, serial = wanted[0]
            # A transport retry must not apply the same XP twice across restarts.
            self.conn.execute('create table if not exists level_training_receipts('
                              'user_id text, rqid text, request_hash text, response_json text,'
                              'primary key(user_id,rqid))')
            cache_key = (str(req.get('user_id', 0)), str(req.get('rqid')))
            fingerprint = hashlib.sha256(json.dumps({k: req.get(k) for k in
                ('gameplayer_id_list', 'item_list', 'is_auto_use_item')}, sort_keys=True).encode()).hexdigest()
            with self.conn:
                if not self.conn.in_transaction:
                    self.conn.execute('BEGIN IMMEDIATE')
                if req.get('rqid') is not None:
                    receipt = self.conn.execute('select request_hash,response_json from level_training_receipts '
                                                'where user_id=? and rqid=?', cache_key).fetchone()
                    if receipt:
                        if receipt[0] not in (fingerprint, req.get('_legacy_level_fingerprint')):
                            raise ValueError('request id reused with different training data')
                        cached = store.player_identity.canonical_request(self.conn, json.loads(receipt[1]))
                        if player_training.restore_use_item_experience(cached, {'id': gid, 'serial': serial}):
                            self.conn.execute('update level_training_receipts set response_json=? '
                                              'where user_id=? and rqid=?',
                                              (json.dumps(cached, ensure_ascii=False), *cache_key))
                        return cached
                p = store.player_by_key(self.conn, gid, serial)
                if p is None:
                    raise ValueError('player not owned')
                amount = player_training.item_experience(req.get('item_list'))
                trained, before, after, applied = player_training.train(p, amount)
                # Update only JSON, preserving identity, acquisition and source metadata.
                self.conn.execute('update gameplayers set json=? where id=? and serial=?',
                                  (json.dumps(trained, ensure_ascii=False), gid, serial))
                store.log_event(self.conn, 'level_training', {
                    'id': gid, 'serial': serial, 'requested_exp': amount, 'applied_exp': applied,
                    'before_level': before['level'], 'after_level': after['level'],
                    'points_added': after['customize_point']-before['customize_point']})
                resp['gameplayer_list'] = [store.wire_player(trained)]
                # CMD_USE_MYCLUB_ITEM reads root experience directly (native
                # 0x79478c4 -> 0x7553b48). The match exp-list adapter is a
                # separate response and cannot update the training screen.
                resp['experience'] = copy.deepcopy(after)
                resp['gameplayer_exp_list'] = [{
                    'gameplayer_id': dict(trained['gameplayer_id']), 'exp_match': applied,
                    'exp_info_prematch': before, 'exp_info_postmatch': after,
                    'exp_extra_from_bonus_effect': 0, 'experience_bonus_type': [],
                    'data_list': [], 'parameters': list(resp['gameplayer_list'][0].get('parameters') or []),
                }]
                resp['result'] = 'NOERR'
                if req.get('rqid') is not None:
                    self.conn.execute('insert into level_training_receipts values (?,?,?,?)',
                                      (*cache_key, fingerprint, json.dumps(resp, ensure_ascii=False)))
        except (ValueError, TypeError, OverflowError) as exc:
            resp['result'] = 'ERR_INVALID_PARAM'
            store.log_event(self.conn, 'level_training_rejected', {'reason': str(exc)})
            self.conn.commit()
        return resp

    def _booster_training(self, resp: dict, req: dict) -> dict:
        """Persist booster crafting for the exact owned (id, serial) instance.

        Inventory remains on the existing offline/template policy. Other item
        types keep their previous response path. No roster is replaced here.
        """
        items = req.get("item_list") or []
        kinds = [(x.get("item") or {}).get("item_type") for x in items if isinstance(x, dict)]
        supported = {"ABILITY_TRAINING_SELECT", "ABILITY_TRAINING_RANDOM"}
        if not any(k in supported for k in kinds):
            return {'msgid':'CMD_USE_MYCLUB_ITEM','rqid':req.get('rqid'),
                    'result':'ERR_INVALID_PARAM','item_list':[]}
        resp = {'msgid':'CMD_USE_MYCLUB_ITEM','rqid':req.get('rqid'),'result':'NOERR','item_list':[]}
        resp.pop("skill_training_result", None)  # corpus item template trained a skill, not a booster
        resp["ability_training_result"] = {"ability_Id": 0, "is_success": "NO"}
        wanted = requested_ids(req.get("gameplayer_id_list"))
        if len(wanted) != 1 or len(kinds) != 1 or kinds[0] not in supported:
            return resp
        gid, serial = wanted[0]
        if serial is None:
            return resp
        player = store.player_by_key(self.conn, gid, serial)
        if player is None:
            return resp
        slot = req.get("training_index", 1)
        if type(slot) is not int or not 0 <= slot < 3:
            slot = 1
        if kinds[0] == "ABILITY_TRAINING_SELECT":
            ability = req.get("ability_id")
            if type(ability) is not int or not 0 < ability <= (255 if slot == 0 else 1023):
                return resp
        else:
            pool = ziru_data.random_boosters()["ability_ids"]
            ability = random.choice(pool)
        params = owned_parameters(player)
        offset = BOOSTER_OFFSET.get(len(params))
        if offset is None or len(params) == 149:
            return resp
        # Canonical innate metadata stays distinct from a crafted active slot.
        raw = ziru_data.player_records().get(str(gid))
        canonical = (raw.get("gameplayer", raw) if raw else {}).get("initial_booster_id_list")
        if isinstance(canonical, list) and len(canonical) == 3:
            player["initial_booster_id_list"] = list(canonical)
        params[offset + slot] = ability
        player["parameters"] = params
        with self.conn:
            store.upsert_player(self.conn, player)
            store.log_event(self.conn, "booster_training", {
                "id": gid, "serial": serial, "slot": slot, "ability_id": ability, "item_type": kinds[0]})
        resp["ability_training_result"] = {"ability_Id": ability, "is_success": "YES"}
        return resp

    # --- ability points (能力点分配) --------------------------------------
    def _position_training(self, resp, req):
        out = {'msgid':'CMD_USE_MYCLUB_ITEM','rqid':req.get('rqid'),'result':'NOERR','item_list':[],
               'achievement_notification': []}
        try:
            out.update(player_operations.transact(self.conn, 'position', req,
                       lambda: player_operations.position(self.conn, req)))
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            out.update(result='ERR_INVALID_PARAM', position_training_result={'additional_position':'','is_success':'NO'})
            store.log_event(self.conn, 'position_training_rejected', {'reason':str(exc)})
            self.conn.commit()
        return out

    def _skill_training(self, resp: dict, req: dict) -> dict:
        resp = {'msgid':req.get('msgid','CMD_USE_MYCLUB_ITEM'),'rqid':req.get('rqid'),
                'result':'NOERR','item_list':[]}
        try:
            resp.update(player_operations.transact(self.conn, 'skill', req,
                        lambda: player_operations.skill(self.conn, req)))
        except (ValueError,TypeError,KeyError,IndexError) as exc:
            resp['result'] = 'ERR_INVALID_PARAM'
            resp['skill_training_result'] = {'additional_skill_id':0,'is_success':'NO'}
            store.log_event(self.conn,'skill_training_rejected',{'reason':str(exc)})
            self.conn.commit()
        return resp

    def _careerplan(self, resp: dict, req: dict) -> dict:
        """Record an allocation.

        The category -> parameter mapping is not pinned yet (TASKS R13), so the
        allocation is logged and mirrored into `careerplan_list[].count` rather
        than inventing a parameter bump. The full request is recorded so the next
        run can pin the field names.
        """
        if 'gameplayer_use_list' in req:
            # Native response contains wrappers in careerplan_result_list, not
            # a frozen player or a bare acknowledgement (ZIRU handler evidence).
            resp = {'msgid':'CMD_USE_MYCLUB_CAREERPLAN','rqid':req.get('rqid'),'result':'NOERR',
                    'careerplan_result_list':[], 'achievement_notification':[]}
            try:
                resp.update(player_operations.transact(self.conn, 'career', req,
                            lambda: player_operations.career(self.conn, req, self._careerplan_list)))
            except (ValueError,TypeError,KeyError,IndexError) as exc:
                resp['result'] = 'ERR_INVALID_PARAM'
                store.log_event(self.conn,'careerplan_rejected',{'reason':str(exc)})
                self.conn.commit()
            return resp
        # Compatibility for the historical log-only request shape. It must
        # never carry a captured account's player mutation results.
        resp = {'msgid':'CMD_USE_MYCLUB_CAREERPLAN','rqid':req.get('rqid'),'result':'NOERR'}
        wanted = requested_ids(req.get("gameplayer_id"))
        category = req.get("careerplan_allocate_type") or req.get("allocate_type")
        points = req.get("allocate_point")
        if not isinstance(points, int):
            points = 0
        store.log_event(self.conn, "careerplan", {
            "asked": wanted, "category": category, "points": points,
            "fields": sorted(k for k in req if k not in ("session_id", "s_keyword")),
        })
        for gid, serial in wanted:
            if points > 0:
                store.record_careerplan(self.conn, gid, serial or 0, category, points)
        self.conn.commit()
        return resp

    # --- draw -------------------------------------------------------------
    def _use_agent(self, resp: dict, req: dict) -> dict:
        """Execute a draw against the pool instead of replaying a capture.

        Before this existed the handler copied whatever gameplayer_list the
        captured response happened to contain (a frozen 10-player list), so
        every pull - regardless of agent_id or count - returned the same IDs
        and every insert hit a primary-key conflict (added: 0).
        """
        agent_id = int(req.get("agent_id") or 0)
        if managed_card_pool.is_managed(self.conn, agent_id):
            try:
                count = req.get("count", 1)
                drawn = managed_card_pool.draw_and_grant(self.conn, agent_id, count, request=req)
            except managed_card_pool.PoolError as error:
                # Closed failure: never replace managed members with corpus/global cards.
                drawn = []
                store.log_event(self.conn, "managed_draw_rejected", {
                    "agent_id": agent_id, "count": count, "reason": str(error)})
                self.conn.commit()
                resp["result"] = "ERR_INVALID_PARAM"
            resp["gameplayer_list"] = [{"gameplayer": store.wire_player(p, "list"),
                "is_warehouse": "YES" if store.in_warehouse(self.conn, p['gameplayer_id']['id'], p['gameplayer_id']['serial']) else "NO"} for p in drawn]
            resp["draft_list"] = []
            # Captured animation/reward records belong to a different draw.
            resp['effect_list'] = []
            resp['present_list'] = []
            resp['achievement_notification'] = []
            resp.pop('void_player_ticket_info', None)
            # Nonempty means "guaranteed headliner" and creates a popup per card.
            resp["pickup_cardtype_list"] = []
            resp["special_gacha_effect_player_id"] = {"id": 0, "serial": 0}
            resp['agent_list'] = managed_card_pool.decorate_agents(self.conn, store.agents(self.conn))
            active = json.loads(store.get_meta(self.conn, 'league_position_agent_ids', '[]'))
            if active:
                by_id = {a['agent_id']: a for a in resp['agent_list']}
                resp['agent_list'] = [by_id[aid] for aid in active if aid in by_id]
            resp['remaining_player_count'] = managed_card_pool.counts(self.conn, agent_id)
            resp['base_player_count'] = managed_card_pool.counts(self.conn, agent_id, remaining=False)
            resp["warehouse_num"] = store.warehouse_count(self.conn)
            return self._currency(resp)

        count = int(req.get("count") or 1)
        if count <= 0:
            count = 1
        drawn = store.draw_players(self.conn, agent_id, count)

        # If the pool is empty we cannot honestly invent players. Fall back to
        # whatever the capture had, but record that the draw was degraded so
        # the condition is visible instead of silently masked.
        degraded = False
        if not drawn:
            drawn = [
                gp.get("gameplayer")
                for gp in (resp.get("gameplayer_list") or [])
                if isinstance(gp, dict) and isinstance(gp.get("gameplayer"), dict)
            ][:count]
            degraded = True

        resp["gameplayer_list"] = [{"gameplayer": p, "is_dummy": "NO"} for p in drawn]

        added = 0
        for p in drawn:
            if store.add_player(self.conn, p, f"draw:{agent_id}"):
                added += 1

        store.log_event(
            self.conn,
            "draw",
            {
                "agent_id": agent_id,
                "count": count,
                "returned": len(drawn),
                "added": added,
                "pool": store.pool_size(self.conn),
                "degraded": degraded,
            },
        )
        self.conn.commit()
        return self._currency(resp)

    # --- ported gameplay surface (TASKS R18) -------------------------------
    # Opponents, league, season, campaign pass and the exchange/rental shops are
    # served from the third-party package's data (work/tools/port_ziru_data.py ->
    # outputs/reference/ziru-data/, read through ziru_data.py).
    #
    # Two rules decide where the payload comes from:
    #   * OUR account's side is always rebuilt here. Any reply that carries our
    #     squad has to describe the club we actually have, or the client plays
    #     with a foreign team (E17).
    #   * A reply that is only about the opponent/league is served as the package
    #     ships it, because those payloads are self-contained (the opponent's 23
    #     players travel inside the same reply) and the client resolves their
    #     slots against it. Rewriting ids in them is what broke the client before.
    #
    # Every handler degrades to the canned capture when the data is not ported,
    # so wiping the port cannot take the login flow down with it.
    VSCOM_PROFILE_COUNT = 100

    def _ported(self, handler: str, fn):
        try:
            return fn()
        except ziru_data.NotPorted as exc:
            store.log_event(self.conn, "ziru_data_missing",
                            {"handler": handler, "error": str(exc)})
            self.conn.commit()
            return None

    def _team_id(self) -> int:
        """Our base team id: what the client last submitted, else 0."""
        want = store.main_squad_id(self.conn)
        squads = store.squads(self.conn)
        squad = next((s for s in squads if int(s.get("squad_id") or 0) == want), None)
        if squad is None and squads:
            squad = squads[0]
        base_team = (squad or {}).get("base_team") or []
        if isinstance(base_team, list) and base_team and isinstance(base_team[0], dict):
            tid = int(base_team[0].get("base_team_id") or 0)
            if tid:
                return tid
        return int(store.get_meta(self.conn, "base_team_id", "0") or 0)

    def _profile_index(self, advance: bool = False) -> int:
        """The match profile in play; `advance` moves to the next opponent."""
        cur = int(store.get_meta(self.conn, "vscom_profile_index", "0") or 0)
        if advance:
            cur = (cur + 1) % self.VSCOM_PROFILE_COUNT
            store.set_meta(self.conn, "vscom_profile_index", str(cur))
        return cur

    def _vscom_opponent(self, mid: str, resp: dict, req: dict) -> dict:
        """Serve one of the package's 30 match profiles.

        Served whole (the reply is self-contained; see the section comment) apart
        from the opponent's identity, which the package fills with its author's
        handle. The profile index is remembered so CMD_START_GAME agrees with the
        opponent the client was just shown.
        """
        try:
            mode = match.opponent_mode(self.conn, req)
        except match.MatchError as exc:
            return match.reject(self.conn, mid, req, exc)
        if mode == match.SEASON:
            return my_league.prepare(self.conn, req, session_token=self._match_session)
        index = self._profile_index(True)
        out = self._ported("vscom_opponent", lambda: ziru_data.opponent_squad(index))
        if out is None:
            return resp
        # The preset squad replaces the canned player list, so the opponent is the
        # squad this division prepared for the profile the cursor just moved to.
        # Development policy (presets fully developed by default, optionally graded
        # by division) is decided inside the builder. When the preset data is
        # missing the canned payload stays untouched.
        squad = opponent_squad.build(self.conn, index)
        if squad:
            out["player_list"] = squad["player_list"]
            if isinstance(squad.get("squad_data"), dict):
                out["squad_data"] = squad["squad_data"]
            out = ziru_data.normalize_opponent_team(out, squad.get("team_id"), squad.get("user_name"))
            owned_coaches.sync_opponent(self.conn, out)
            if squad.get("substituted"):
                store.log_event(self.conn, "opponent_squad_substituted", {
                    "profile": squad["profile_index"], "slots": squad["substituted"]})
            if squad.get("xi_swaps"):
                store.log_event(self.conn, "opponent_squad_xi_compiled", {
                    "profile": squad["profile_index"], "swaps": squad["xi_swaps"]})
        else:
            store.log_event(self.conn, "opponent_squad_unavailable", {
                "profile": index, "reason": opponent_squad.last_error()})
        out["rqid"] = req.get("rqid")
        out["msgid"] = mid
        out["result"] = "NOERR"
        out["side"] = "AWAY"
        if req.get("cpu_level"):
            out["cpu_level"] = req["cpu_level"]
        if mode == match.AI:
            # Profile selection writes its cursor; finish that transaction before
            # allocating the independently locked league match/receipt state.
            self.conn.commit()
            out = ai_division.prepare(self.conn, out, req, session_token=self._match_session)
        elif mode in (match.OTHER, match.LOCAL):
            # Only a locally served computer opponent creates a tracked
            # ordinary match. A category name alone never grants this state.
            self.conn.commit()
            out = local_vscom.prepare(self.conn, out, req, session_token=self._match_session)
        # E59: remember whose kits the away side must wear at CMD_START_GAME - the
        # captured template there carries the capture-time opponent's kits.
        away_team = int((out.get("team_data") or {}).get("base_team_id") or 0)
        if away_team:
            store.set_meta(self.conn, "vscom_opponent_team_id", str(away_team))
        store.log_event(self.conn, "vscom_opponent", {
            "profile": self._profile_index(),
            "team": (out.get("team_data") or {}).get("team_name"),
            "players": len(out.get("player_list") or []),
            "category": req.get("match_category"),
        })
        self.conn.commit()
        return out

    def _start_game(self, resp: dict, req: dict) -> dict:
        """CMD_START_GAME: refresh the home side with the squad we actually have.

        The captured reply is the authoritative schema for this command *and* it
        supplies the base uniforms, stadium and commentary scaffolding. Refresh
        the players, coach and tactics from the selected saved game plan so a
        historical capture cannot replace the user's current selections.

        `squad_data_away` stays null because the official reply has it null too
        (measured: both our capture and the package agree) - the away side comes
        from the CMD_GET_VSCOM_OPPONENT reply served just before, which is why
        that reply's game_id is echoed here.
        """
        mode = match.resolve_mode(self.conn, req, include_receipts=False)
        squad_id = int(req.get("squad_id") or store.main_squad_id(self.conn))
        squad = next((s for s in store.squads(self.conn)
                      if int(s.get("squad_id") or 0) == squad_id), {})
        saved_plan = squad.get("squad_data") or {}
        home = self._home_entries(req.get("player_list_home") or saved_plan.get("player_list"),
                                  log_substitutes=mode not in (match.AI, match.LOCAL))
        if not home:
            if mode in (match.AI, match.LOCAL):
                raise match.MatchError('empty_match_squad')
            return resp
        squad_home = dict(resp.get("squad_data_home") or {})
        squad_home["player_list"] = home
        # START_GAME carries a full coach object, whereas the saved plan only
        # carries its (id, serial) reference. The captured starter coach must
        # never override the manager the player selected for this match.
        coach = owned_coaches.resolve(self.conn, saved_plan.get("coach"))
        if coach:
            squad_home["coach"] = coach
        team = store.base_team_id_of(squad.get("base_team")) or self._team_id()
        if team:
            squad_home["team_id"] = team
        if saved_plan.get("squad_gameplan"):
            squad_home["squad_gameplan"] = copy.deepcopy(saved_plan["squad_gameplan"])
        resp["squad_data_home"] = squad_home
        # the kits the club actually picked (CMD_SET_MYCLUB_BRING_UNIFORM), or the
        # current base team's own pair; the captured template made every match wear
        # the captured club's kits (user report 2026-10-15)
        kits = store.equipped_uniforms(self.conn)
        if kits and isinstance(resp.get("uniform_list_home"), dict):
            resp["uniform_list_home"]["uniform_list"] = list(kits)
        if team and isinstance(resp.get("uniform_list_home"), dict):
            resp["uniform_list_home"]["base_team_id"] = team
            if team != store.club_team_id(self.conn):
                resp["uniform_list_home"]["uniform_list"] = store.default_uniforms(team)
        # E59: the away side wears the opponent's own kits. The captured template
        # hardcoded the capture-time opponent's (CA Boca Juniors, team 139), so
        # every match showed that kit and its crest (user report 2026-10-15).
        away_team = int(store.get_meta(self.conn, "vscom_opponent_team_id", "0") or 0)
        if away_team and isinstance(resp.get("uniform_list_away"), dict):
            resp["uniform_list_away"]["base_team_id"] = away_team
            resp["uniform_list_away"]["uniform_list"] = [away_team * store.UNIFORM_STRIDE + slot
                                                         for slot in (0, 1, 2)]
        # Keep commentary aligned with the current division. Actual difficulty
        # is served by the opponent's cpu_level and league com_level_info;
        # pre_division alone is not evidence of an engine difficulty setting.
        if mode == match.AI:
            state = ai_division.state(self.conn)
            commentary = resp.get("commentary_info")
            if (isinstance(commentary, dict) and "pre_division" in commentary
                    and state.get("division")):
                commentary["pre_division"] = int(state["division"])
        if match.match_id(req, required=False):
            resp["game_id"] = match.match_id(req, required=False)
        # the commentary pre-match list addresses the same players by reference
        commentary = resp.get("commentary_info")
        if isinstance(commentary, dict) and "first_match_player_list" in commentary:
            commentary["first_match_player_list"] = [dict(e["gameplayer_id"]) for e in home]
        # Persist only after constructing the actual roster the game receives.
        # A rejected/empty squad must not leave a phantom started match.
        if mode == match.AI and not ai_division.start(self.conn, req, home):
            return {'msgid': 'CMD_START_GAME', 'rqid': req.get('rqid'),
                    'result': 'ERR_INVALID_PARAM'}
        if mode == match.LOCAL and not local_vscom.start(self.conn, req, home, team_id=team):
            return {'msgid': 'CMD_START_GAME', 'rqid': req.get('rqid'),
                    'result': 'ERR_INVALID_PARAM'}
        store.log_event(self.conn, "start_game", {
            "game_id": resp.get("game_id"),
            "home": len(home),
            "away_refs": len(req.get("player_list_away") or []),
            "category": req.get("match_category"),
        })
        self.conn.commit()
        return resp

    def _home_entries(self, refs, log_substitutes=True) -> list[dict]:
        """Turn the client's home squad references into player objects.

        The request carries `player_list_home` as {gameplayer_id} pairs, so the
        reply has to answer with the matching objects. An id we cannot resolve
        (the client still holding an id we have since replaced) falls back to a
        squad candidate rather than dropping the slot - a short team is a worse
        failure than a substituted player, and the substitution is logged.
        """
        entries: list[dict] = []
        used: set[tuple[int, int]] = set()
        substitutes = 0

        def emit(player: dict) -> None:
            key = store.player_key(player)
            used.add(key)
            params = player_skills.match_parameters(player, owned_parameters(player))
            entries.append({
                "gameplayer_id": {"id": key[0], "serial": key[1]},
                "parameters": params,
                "averaged_overall": 0,
            })

        for gid, serial in squad_refs(refs):
            player = store.player_by_key(self.conn, gid, serial)
            if player is None and not store.player_identity.enabled(self.conn):
                player = store.player_by_key(self.conn, gid)
            if player and store.player_key(player) not in used:
                emit(player)
            else:
                if store.player_identity.enabled(self.conn):
                    raise ValueError('Unknown or duplicate owned player instance in match squad')
                substitutes += 1

        if substitutes:
            for candidate in store.squad_candidates(self.conn):
                if substitutes <= 0:
                    break
                if store.player_key(candidate) in used:
                    continue
                emit(candidate)
                substitutes -= 1
            if log_substitutes:
                store.log_event(self.conn, "start_game_substitute", {"count": substitutes})
        return entries

    def _season_schedule(self, resp: dict, req: dict) -> dict:
        """The league fixture list for our team, out of the ported seasons.

        Our own capture of this command is the schema base (identical shape:
        38 fixtures x 10 matches) and the fixture list is replaced with the
        ported season that actually contains our team, so the table the client
        shows matches the fixtures it is given.
        """
        name, sch = self._ported("season_schedule",
                                 lambda: ziru_data.schedule_for_team(self._team_id())) or (None, None)
        if not sch:
            return resp
        resp["schedule_list"] = sch.get("schedule_list") or []
        store.log_event(self.conn, "season_schedule", {
            "season": name, "fixtures": len(resp["schedule_list"]), "team": self._team_id(),
        })
        self.conn.commit()
        return resp

    def _season_info(self, resp: dict, req: dict) -> dict:
        """Point the season snapshot at the opponent we will actually field."""
        idx = self._profile_index()
        prof = self._ported("season_info", lambda: ziru_data.profile(idx))
        if prof:
            resp["opponent_team_id"] = int(prof.get("base_team_id") or 0)
            resp["user_side"] = "HOME"
        resp["fixture"] = int(resp.get("fixture") or 1)
        store.log_event(self.conn, "season_info", {
            "profile": idx, "opponent_team_id": resp.get("opponent_team_id"),
        })
        self.conn.commit()
        return resp

    def _campaign_pass(self, resp: dict, req: dict) -> dict:
        """Claim the campaign-pass reward: record the claim, pay out the currency.

        The package's payload is the base (this command was never captured from
        the official server), with the claim count coming from our store so the
        progress the client draws is ours.
        """
        out = self._ported("campaign_pass", ziru_data.campaign_pass)
        if out is None:
            return resp
        claimed = store.campaign_pass_received(self.conn) + 1
        store.set_campaign_pass_received(self.conn, claimed)
        out["received_progress_info"] = dict(out.get("received_progress_info") or {})
        out["received_progress_info"]["count"] = claimed
        out["rqid"] = req.get("rqid")
        store.log_event(self.conn, "campaign_pass", {
            "claimed": claimed,
            "rewards": len(out.get("receive_result_list") or []),
        })
        self.conn.commit()
        return out

    def _exchange_item(self, resp: dict, req: dict) -> dict:
        """Spend season points on an item.

        The cost comes from the request (this command has never been captured, so
        there is no authoritative field name to read it from - `point`, `cost` and
        `item_info.point` are all accepted). A balance that does not cover the
        cost is refused outright instead of half-applying the exchange.
        """
        cost = self._requested_cost(req)
        out = self._ported("exchange_item", ziru_data.exchange_item)
        if out is None:
            return resp
        ok = store.spend_season_points(self.conn, cost) if cost else True
        out["exchange_result"] = "SUCCESS" if ok else "NOT_ENOUGH_POINT"
        out["point"] = store.season_points(self.conn)
        out["rqid"] = req.get("rqid")
        store.log_event(self.conn, "season_exchange_item", {
            "cost": cost, "ok": ok, "item": req.get("item_id"),
            "balance": out["point"],
        })
        self.conn.commit()
        return out

    def _exchange_rental(self, resp: dict, req: dict) -> dict:
        """Rent a player with season points.

        The package's payload holds a template player (serial 0, base-player id),
        which is not something the client can resolve, so the rented card is
        granted through the club store instead and the reply carries our wired
        card. If no card can be resolved the exchange is refused and logged -
        handing back a template would put an unusable card in the squad.
        """
        out = self._ported("exchange_rental", ziru_data.exchange_rental)
        if out is None:
            return resp
        item_id = int(req.get("item_id") or (req.get("item_info") or {}).get("item_id") or 0)
        cost = self._requested_cost(req) or self._rental_cost(item_id)
        card = self._rental_card(req, item_id)
        if card is None:
            out["result"] = "NOERR"
            out["exchange_result"] = "NOT_FOUND"
            out["point"] = store.season_points(self.conn)
            out["rqid"] = req.get("rqid")
            store.log_event(self.conn, "season_exchange_rental_refused",
                            {"item_id": item_id, "cost": cost})
            self.conn.commit()
            return out
        if cost and not store.spend_season_points(self.conn, cost):
            out["exchange_result"] = "NOT_ENOUGH_POINT"
            out["point"] = store.season_points(self.conn)
            out["rqid"] = req.get("rqid")
            store.log_event(self.conn, "season_exchange_rental_refused",
                            {"item_id": item_id, "cost": cost, "reason": "no points"})
            self.conn.commit()
            return out

        store.add_player(self.conn, card, f"rental:{item_id}")
        expiry = int(out.get("expiration_date") or 0) or int(time.time()) + 30 * 86400
        key = store.add_rental(self.conn, card, item_id, cost, expiry)
        out["gameplayer"] = store.wire_player(card, "login")
        out["exchange_result"] = "SUCCESS"
        out["expiration_date"] = expiry
        out["point"] = store.season_points(self.conn)
        out["rqid"] = req.get("rqid")
        store.log_event(self.conn, "season_exchange_rental", {
            "item_id": item_id, "cost": cost, "card": key, "expires": expiry,
        })
        self.conn.commit()
        return out

    def _season_point_list(self, mid: str, resp: dict, req: dict) -> dict:
        """The point shops: our balance, and which rentals we already hold."""
        rel = "rental_list" if "RENTALPLAYER_LIST" in mid else "point_item_list"
        out = self._ported(rel, lambda: ziru_data.rental_list() if rel == "rental_list"
                           else ziru_data.point_item_list())
        if out is None:
            return resp
        out["point"] = store.season_points(self.conn)
        out["rqid"] = req.get("rqid")
        if rel == "rental_list":
            # the shop lists items, so "already rented" is an item_id question -
            # matching on the card id would never fire (the list carries the
            # package's template player, not the card we granted).
            held = {r["item_id"] for r in store.rentals(self.conn)}
            for entry in out.get("rental_player_list") or []:
                entry["is_rent"] = "YES" if int((entry or {}).get("item_id") or 0) in held else "NO"
        return out

    @staticmethod
    def _requested_cost(req: dict) -> int:
        """Season points a request wants to spend, from whichever field it used."""
        for candidate in (req.get("point"), req.get("cost"), req.get("use_point"),
                          (req.get("item_info") or {}).get("point")):
            try:
                value = int(candidate)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return 0

    def _rental_cost(self, item_id: int) -> int:
        """The listed price of a rental item (the ported shop is authoritative)."""
        data = self._ported("rental_list", ziru_data.rental_list)
        if not data:
            return 0
        for entry in data.get("rental_player_list") or []:
            if int((entry or {}).get("item_id") or 0) == item_id:
                return int(entry.get("point") or 0)
        return 0

    def _rental_card(self, req: dict, item_id: int) -> dict | None:
        """Which card a rental grants: the one asked for, else the template's.

        Looked up in owned + draw pool, because a rental shop is exactly where a
        card you do not own yet becomes playable. The package's template id is a
        base-player id rather than a card id, so it only helps when it happens to
        resolve; otherwise the exchange is refused rather than faked.
        """
        index = ziru_data.card_index(self.conn)
        for gid, _serial in squad_refs(req.get("gameplayer_id") or req.get("target_player")):
            if gid in index:
                return index[gid]
        data = self._ported("rental_list", ziru_data.rental_list)
        for entry in (data or {}).get("rental_player_list") or []:
            if int((entry or {}).get("item_id") or 0) != item_id:
                continue
            gid = int(((entry.get("player_info") or {}).get("gameplayer_id") or {}).get("id") or 0)
            if gid in index:
                return index[gid]
        return None

    # --- reporting ---------------------------------------------------------
    def status(self) -> str:
        return store.summary(self.conn)
