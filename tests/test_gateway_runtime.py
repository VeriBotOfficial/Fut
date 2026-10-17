import importlib
from pathlib import Path
import socket
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway/work/tools'))
import gateway_runtime


class GatewayRuntimeTest(unittest.TestCase):
    def test_os_port_remains_free_of_existing_listener(self):
        # A previous/other app may own a fixed local port. Binding zero lets
        # the kernel allocate a different listener without closing that app.
        with socket.socket() as occupied, socket.socket() as current:
            occupied.bind(('127.0.0.1',0));occupied.listen(1)
            current.bind(('127.0.0.1',0));current.listen(1)
            old=occupied.getsockname()[1];port=current.getsockname()[1]
            self.assertNotEqual(old,port)
            value=gateway_runtime.rewrite_live_urls({'livedata_list':[{'name':'folder/data.bin','url':'https://old/data.bin'}]},port)
            self.assertEqual(value['livedata_list'][0]['url'],f'http://127.0.0.1:{port}/livedata/data.bin')
            with socket.create_connection(('127.0.0.1',port),timeout=1):
                accepted,_=current.accept();accepted.close()
            with socket.create_connection(('127.0.0.1',old),timeout=1):
                accepted,_=occupied.accept();accepted.close()

    def test_only_live_data_addresses_change_without_input_mutation(self):
        source={'other_url':'http://127.0.0.1:8085/livedata/x','nested':[{'livedata_list':[
            {'name':'dir/data.bin','url':'https://cdn/x','sha256':'abc'},None,{'name':''}]}]}
        import copy
        before=copy.deepcopy(source)
        result=gateway_runtime.rewrite_live_urls(source,43127)
        self.assertEqual(source,before)
        self.assertEqual(result['other_url'],source['other_url'])
        self.assertEqual(result['nested'][0]['livedata_list'][0],{'name':'dir/data.bin','url':'http://127.0.0.1:43127/livedata/data.bin','sha256':'abc'})

    def test_port_read_once_for_large_response_and_hook_is_idempotent(self):
        module=types.SimpleNamespace(_local_urls=lambda value:value)
        ports=[]
        def reader():ports.append(1);return 49152
        self.assertTrue(gateway_runtime.install(module,reader))
        self.assertFalse(gateway_runtime.install(module,reader))
        result=module._local_urls({'players':[{'parameters':list(range(147))}for _ in range(100)]})
        self.assertEqual(len(result['players']),100)
        self.assertEqual(len(ports),1)

    def test_restarted_listener_is_not_cached(self):
        module=types.SimpleNamespace(_local_urls=lambda value:value)
        ports=iter([40001,40002])
        gateway_runtime.install(module,lambda:next(ports))
        value={'livedata_list':[{'name':'version.bin'}]}
        self.assertIn(':40001/',module._local_urls(value)['livedata_list'][0]['url'])
        self.assertIn(':40002/',module._local_urls(value)['livedata_list'][0]['url'])

    def test_invalid_listener_rejected(self):
        for port in (0,-1,65536,True,'8085'):
            with self.subTest(port=port),self.assertRaises(ValueError):gateway_runtime.rewrite_live_urls({},port)

    def test_host_seed_has_no_android_dependency(self):
        with patch.dict(sys.modules,{'embedded_gateway':None}):self.assertFalse(gateway_runtime.install())


if __name__=='__main__':unittest.main()
