"""One persisted source for the account's MANUAL/AUTO progression setting."""
import club_store as store

META_KEY = 'careerplan_allocate_type'
COMMAND = 'CMD_SET_MYCLUB_CAREERPLAN_ALLOCATE_TYPE'
VALID_MODES = frozenset({'MANUAL', 'AUTO'})


def get(conn) -> str:
    mode = store.get_meta(conn, META_KEY, 'AUTO')
    return mode if mode in VALID_MODES else 'AUTO'


def handle(conn, request: dict) -> dict:
    mode = request.get('careerplan_allocate_type', request.get('allocate_type'))
    reply = {'msgid': COMMAND, 'rqid': request.get('rqid'), 'result': 'NOERR'}
    if not isinstance(mode, str) or mode not in VALID_MODES:
        reply['result'] = 'ERR_INVALID_PARAM'
        return reply
    # Setting a value is already idempotent. Unlike a random draw it needs no
    # receipt containing an old response which could resurrect a previous mode.
    with conn:
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        store.set_meta(conn, META_KEY, mode)
        store.log_event(conn, 'training_mode_saved', {'mode': mode})
    reply['careerplan_allocate_type'] = mode
    return reply


def apply(conn, response: dict) -> dict:
    mode = get(conn)
    entry = response.get('entry_info')
    if isinstance(entry, dict):
        entry['careerplan_allocate_type'] = mode
    if 'careerplan_allocate_type' in response:
        response['careerplan_allocate_type'] = mode
    return response
