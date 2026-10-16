"""最简 telnet 客户端，适配 eNSP 串口重定向。

Python 3.13+ 已移除标准库 telnetlib（PEP 594），这里用原始 socket 实现：
- 应答 IAC 协商（一律 WONT/DONT 拒绝选项）
- 串口方式无需登录，连接后发送回车唤醒命令行
- read_until 按正则等设备提示符（如 <R1>），并处理 "---- More ----" 分页
"""
import re
import socket
import time

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
# 用户视图提示符 <R1>，系统视图 [R1]，均作为命令结束标志
PROMPT_RE = r'[<\[][\w\-.*]+[>\]]'


class TelnetError(Exception):
    pass


class SerialTelnet:
    def __init__(self, host, port, timeout=10):
        self.timeout = timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(0.5)
        self.buf = b''

    def _negotiate(self, data):
        """剥离 IAC 序列并回应拒绝，返回净数据"""
        out = bytearray()
        i = 0
        while i < len(data):
            if data[i] == IAC and i + 1 < len(data):
                cmd = data[i + 1]
                if cmd in (DO, WILL) and i + 2 < len(data):
                    opt = data[i + 2]
                    reply = bytes([IAC, WONT if cmd == DO else DONT, opt])
                    try:
                        self.sock.sendall(reply)
                    except OSError:
                        pass
                    i += 3
                elif cmd in (DONT, WONT) and i + 2 < len(data):
                    i += 3
                elif cmd == SB:
                    j = data.find(bytes([IAC, SE]), i)
                    i = len(data) if j == -1 else j + 2
                elif cmd == IAC:
                    out.append(IAC)
                    i += 2
                else:
                    i += 2
            else:
                out.append(data[i])
                i += 1
        return bytes(out)

    def read_until(self, pattern=PROMPT_RE, timeout=None):
        """读到匹配 pattern（默认设备提示符）为止，返回已读文本"""
        deadline = time.time() + (timeout or self.timeout)
        rx = re.compile(pattern.encode() if isinstance(pattern, str) else pattern)
        while time.time() < deadline:
            m = rx.search(self.buf)
            if m:
                text = self.buf[:m.end()].decode('utf-8', 'ignore')
                self.buf = self.buf[m.end():]
                return text
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise TelnetError('连接被对端关闭')
            except socket.timeout:
                continue
            self.buf += self._negotiate(chunk)
            if b'---- More ----' in self.buf:      # 分页则送空格翻页
                self.buf = self.buf.replace(b'---- More ----', b'')
                self.buf = self.buf.replace(b'\x1b[42D', b'')
                self.sock.sendall(b' ')
        raise TelnetError(f'读取超时，未等到提示符 {pattern!r}')

    def write_line(self, s=''):
        # 只发 \r：eNSP 串口把 \n 也当一次回车，会重复执行上一条命令导致输出错位
        self.sock.sendall(s.encode() + b'\r')

    def execute(self, cmd, timeout=None):
        """发送一条命令并读到提示符，返回输出文本"""
        self.write_line(cmd)
        return self.read_until(PROMPT_RE, timeout)

    def wake(self):
        """串口重定向无登录过程，回车唤醒并读到第一个提示符"""
        self.write_line()
        return self.read_until(PROMPT_RE)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
