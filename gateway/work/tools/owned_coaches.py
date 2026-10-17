"""Serve owned coaches in each entry endpoint's captured identity shape."""
import copy

import club_store as store


def resolve(conn, selection):
    """Expand a saved coach reference into the match protocol's full object."""
    if isinstance(selection, list):
        selection = selection[0] if selection else None
    if not isinstance(selection, dict):
        return None
    identity = selection.get('coach_id')
    if not isinstance(identity, dict):
        return None
    owned = store.coaches(conn)
    coach = next((c for c in owned if c.get('coach_id') == identity), None)
    if coach is None:
        # Captured opponent serials belong to the original account. The coach
        # definition is keyed by id; keep the selected instance's wire identity.
        coach = next((c for c in owned if (c.get('coach_id') or {}).get('id') == identity.get('id')), None)
    if coach is None:
        return None
    out = copy.deepcopy(coach)
    out['coach_id'] = copy.deepcopy(identity)
    return out


def sync_opponent(conn, response):
    """The squad reference and coach_data must describe the same manager."""
    selection = (response.get('squad_data') or {}).get('coach')
    coach = resolve(conn, selection)
    if coach is not None:
        response['coach_data'] = coach
    return response


def apply(conn, entry):
    owned = store.coaches(conn)
    if not owned:
        return
    template = next((c for c in entry.get('coach_list') or [] if isinstance(c, dict)), {})
    scalar_id = isinstance(template.get('coach_id'), int)
    result = []
    for coach in owned:
        # Legacy entry commands require their template's extra fields and an
        # integer coach_id; login uses the modern raw payload with (id, serial).
        out = copy.deepcopy(template)
        out.update(copy.deepcopy(coach))
        if 'is_locked' in template:
            out['is_locked'] = coach.get('is_lock', template['is_locked'])
        if scalar_id:
            out['coach_id'] = coach['coach_id']['id']
        result.append(out)
    entry['coach_list'] = result
    if 'coach_list_num' in entry:
        entry['coach_list_num'] = len(result)
