"""单台设备的一轮采集：telnet 登录 eNSP 串口，执行 display 命令并解析。

返回与 device_status 表对应的 dict；任何异常都收敛为 online=0 + error 说明，
绝不上抛，保证一台设备故障不影响其他设备的采集。
"""
import socket

from . import parsers
from .telnet_client import SerialTelnet, TelnetError

CMD_VRRP = 'display vrrp brief'
CMD_OSPF = 'display ospf peer brief'
CMD_INTF = 'display interface brief'
CMD_ROUTE = 'display ip routing-table'


def collect_device(dev, timeout=10):
    """dev 为 devices 表行（sqlite3.Row 或 dict）。返回采集结果 dict。"""
    result = {
        'online': 0, 'vrrp_state': '[]', 'ospf_neighbors': 0, 'ospf_full': 0,
        'ospf_state': '[]', 'interfaces': '[]', 'route_count': 0,
        'biz_ping_ok': -1, 'error': '',
    }
    try:
        with SerialTelnet(dev['host'], dev['port'], timeout=timeout) as tn:
            tn.wake()
            try:
                tn.execute('screen-length 0 temporary')   # 关闭分页，失败也不影响
            except TelnetError:
                pass
            vrrp_out = tn.execute(CMD_VRRP)
            ospf_out = tn.execute(CMD_OSPF)
            intf_out = tn.execute(CMD_INTF)
            route_out = tn.execute(CMD_ROUTE)

            vrrp = parsers.parse_vrrp(vrrp_out)
            peers = parsers.parse_ospf_peers(ospf_out)
            intfs = parsers.parse_interfaces(intf_out)

            result['online'] = 1
            result['vrrp_state'] = parsers.json_dumps(vrrp)
            result['ospf_state'] = parsers.json_dumps(peers)
            result['ospf_neighbors'] = len(peers)
            result['ospf_full'] = sum(1 for p in peers if p['state'] == 'Full')
            result['interfaces'] = parsers.json_dumps(intfs)
            result['route_count'] = parsers.parse_route_count(route_out)

            target = (dev['ping_target'] or '').strip()
            if target:
                try:
                    ping_out = tn.execute(f'ping -c 3 {target}', timeout=max(timeout, 20))
                    result['biz_ping_ok'] = 1 if parsers.parse_ping_ok(ping_out) else 0
                except TelnetError:
                    result['biz_ping_ok'] = 0
    except (TelnetError, OSError, socket.timeout) as e:
        result['error'] = f'{type(e).__name__}: {e}'
    return result
