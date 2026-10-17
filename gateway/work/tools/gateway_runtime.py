"""Keep Python live-data URLs on the same dynamically bound Java listener."""
import functools
import sys
from pathlib import Path


def _android_port():
    from java import jclass
    port = int(jclass('org.efootballrevival.bootstrap.GatewayGuard').port())
    if not 1 <= port <= 65535:
        raise RuntimeError('local gateway listener is not ready')
    return port


def rewrite_live_urls(value, port):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('invalid local listener port')
    if isinstance(value, dict):
        result = {key: rewrite_live_urls(item, port) for key, item in value.items()}
        if isinstance(result.get('livedata_list'), list):
            for item in result['livedata_list']:
                if isinstance(item, dict) and item.get('name'):
                    item['url'] = 'http://127.0.0.1:' + str(port) + '/livedata/' + Path(item['name']).name
        return result
    if isinstance(value, list):
        return [rewrite_live_urls(item, port) for item in value]
    return value


def install(module=None, port_reader=None):
    """Called after save migration, before the first game request.

    The upstream bridge's exact recursive live-data transformation is retained
    with a listener-derived prefix. This mirrors the shipped Python 3.12 bytecode
    (_local_urls), while resolving the port only once per response. Host-side seed
    construction has no embedded_gateway module and installs no Android hook.
    """
    module = module if module is not None else sys.modules.get('embedded_gateway')
    if module is None:
        return False
    original = getattr(module, '_local_urls', None)
    if not callable(original):
        raise RuntimeError('embedded bridge live-data entry missing')
    if getattr(original, '_efootxi_dynamic_port', False):
        return False
    reader = port_reader or _android_port
    @functools.wraps(original)
    def local_urls(value):
        return rewrite_live_urls(value, reader())
    local_urls._efootxi_dynamic_port = True
    module._local_urls = local_urls
    return True
