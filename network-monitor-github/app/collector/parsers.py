"""华为 VRP display 命令输出的正则解析。

解析目标（与 device_status 表字段对应）：
- display vrrp brief           -> [{vrid, state, interface, vip}]
- display ospf peer brief      -> [{area, interface, neighbor, state}]
- display interface brief      -> [{name, phy, protocol}]
- display ip routing-table     -> 路由条目数
- ping 输出                     -> 是否收到应答
"""
import json
import re

_VRRP_LINE = re.compile(
    r'^\s*(\d+)\s+(Master|Backup|Initialize)\s+(\S+)\s+\S+\s+(\d+\.\d+\.\d+\.\d+)',
    re.M)
_OSPF_LINE = re.compile(
    r'^\s*(\d+\.\d+\.\d+\.\d+)\s+(\S+)\s+(\d+\.\d+\.\d+\.\d+)\s+([\w-]+)', re.M)
_INTF_LINE = re.compile(
    r'^\s*(\S+)\s+(up|down|\*down)(?:\(\w+\))?\s+(up|down|\*down)(?:\(\w+\))?\s', re.M)
_ROUTE_LINE = re.compile(r'^\s*\d+\.\d+\.\d+\.\d+/\d+\s+\S+\s+\d+', re.M)
_PING_RECV = re.compile(r'(\d+)\s+packet\(s\)\s+received')


def parse_vrrp(text):
    """display vrrp brief -> [{'vrid':10,'state':'Master','interface':'Vlanif10','vip':...}]"""
    return [{'vrid': int(m[0]), 'state': m[1], 'interface': m[2], 'vip': m[3]}
            for m in _VRRP_LINE.findall(text)]


def parse_ospf_peers(text):
    """display ospf peer brief -> [{'area':..., 'interface':..., 'neighbor':..., 'state':'Full'}]"""
    return [{'area': m[0], 'interface': m[1], 'neighbor': m[2], 'state': m[3]}
            for m in _OSPF_LINE.findall(text)]


def parse_interfaces(text):
    """display interface brief -> [{'name':'GE0/0/0','phy':'up','protocol':'up'}]"""
    return [{'name': m[0], 'phy': m[1], 'protocol': m[2]}
            for m in _INTF_LINE.findall(text)]


def parse_route_count(text):
    """display ip routing-table -> 路由条目数"""
    return len(_ROUTE_LINE.findall(text))


def json_dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def parse_ping_ok(text):
    """ping 输出 -> True/False（至少收到1个应答即通）"""
    m = _PING_RECV.search(text)
    return bool(m and int(m.group(1)) >= 1)
