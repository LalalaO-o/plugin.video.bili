# -*- coding:utf-8 -*-
"""B站直播弹幕 ASS 叠加 (从 Mephis 移植)。

流程：
1. WBI 签名请求 getDanmuInfo 获取 token + host_list
2. ws:// 连接弹幕服务器，发送认证包（含 token + buvid）
3. 实时接收 DANMU_MSG → 构造 danmaku2ass 格式 → 写盘 + 周期性 setSubtitles
   触发 libass 重读（让 Kodi 渲染最新弹幕）
"""
import base64
import io
import json
import os
import re
import socket
import struct
import threading
import time
import zlib

import xbmc

from danmaku2ass import ProcessComments, CalculateLength


_ws_send_count = 0


def _ws_send(sock, data):
    global _ws_send_count
    if isinstance(data, str):
        data = data.encode('utf-8')
    frame = bytearray([0x82])
    length = len(data)
    if length < 126:
        frame.append(0x80 | length)
    elif length < 65536:
        frame.append(0x80 | 126)
        frame.extend(struct.pack('>H', length))
    else:
        frame.append(0x80 | 127)
        frame.extend(struct.pack('>Q', length))
    mask_key = os.urandom(4)
    frame.extend(mask_key)
    frame.extend(bytes(b ^ mask_key[i % 4] for i, b in enumerate(data)))
    sock.send(bytes(frame))
    _ws_send_count += 1


def _bili_packet(opcode, body=b''):
    if isinstance(body, str):
        body = body.encode('utf-8')
    return struct.pack('>IHHII', 16 + len(body), 16, 1, opcode, 1) + body


class LiveDanmakuClient:
    def __init__(self, room_id, ass_path, uid=0, cookie='',
                 font_size=25, opacity=1.0, stay_time=8, display_area=1.0,
                 buvid=''):
        self.room_id      = int(room_id)
        self.ass_path     = ass_path
        self.uid          = int(uid) if uid else 0
        self._cookie      = cookie
        self._buvid       = buvid
        self.font_size    = float(font_size)
        self._opacity     = float(opacity)
        self.stay_time    = float(stay_time)
        self.display_area = float(display_area)

        self.running      = False
        self._connected   = False
        self.sock         = None
        self._start_time  = time.time()
        self.danmaku_list = []
        self.lock         = threading.Lock()

    def _get_token_wbi(self):
        try:
            from addon import getWbiKeys, encWbi
            import requests
            params = encWbi({'id': str(self.room_id), 'type': '0'}, *getWbiKeys())
            full_url = 'https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo'
            h = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://live.bilibili.com/',
            }
            if self._cookie:
                h['Cookie'] = self._cookie
            r = requests.get(full_url, params=params, headers=h, timeout=10)
            data = r.json()
            xbmc.log('[live.danmaku] getDanmuInfo code=%s' % data['code'], xbmc.LOGDEBUG)
            if data['code'] == 0:
                return data['data']
        except Exception as e:
            xbmc.log('[live.danmaku] _get_token_wbi: %s' % str(e), xbmc.LOGWARNING)
        return None

    def _connect(self, host, port):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(8)
            sock.connect((host, port))
            key = base64.b64encode(os.urandom(16)).decode()
            req = (
                f'GET /sub HTTP/1.1\r\nHost: {host}\r\n'
                f'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                f'Sec-WebSocket-Key: {key}\r\n'
                f'Sec-WebSocket-Version: 13\r\n\r\n'
            )
            sock.send(req.encode())
            resp = b''
            while b'\r\n\r\n' not in resp:
                chunk = sock.recv(4096)
                if not chunk:
                    sock.close()
                    return None
                resp += chunk
            if b'101' not in resp:
                sock.close()
                return None
            self.sock = sock
            xbmc.log('[live.danmaku] ws connected %s:%s' % (host, port), xbmc.LOGDEBUG)
            return sock
        except Exception as e:
            xbmc.log('[live.danmaku] _connect: %s' % str(e), xbmc.LOGWARNING)
            return None

    def _send_auth(self, token):
        body = json.dumps({
            'uid':      self.uid,
            'roomid':   self.room_id,
            'protover': 2,
            'buvid':    self._buvid or '',
            'platform': 'web',
            'type':     2,
            'key':      token,
        })
        _ws_send(self.sock, _bili_packet(7, body))
        xbmc.log('[live.danmaku] auth sent', xbmc.LOGDEBUG)

    def _handle_message(self, body):
        msg = json.loads(body.decode('utf-8', errors='replace'))
        if msg.get('cmd') != 'DANMU_MSG':
            return
        info = msg.get('info', [])
        if len(info) < 3:
            return
        meta = info[0]
        if not isinstance(meta, list) or len(meta) < 4:
            return

        mode     = int(meta[1]) if len(meta) > 1 else 1
        fontsize = int(meta[2]) if len(meta) > 2 else 25
        color    = int(meta[3]) if len(meta) > 3 else 0xffffff

        text = str(info[1]) if info[1] else ''
        if not text.strip():
            return

        text = re.sub('[\x00-\x08\x0b\x0c\x0e-\x1f]', '\ufffd', text)
        text = text.replace('\n', '/')
        text = text.replace('\r', '')

        pos_map = {1: 0, 5: 1, 4: 2}
        pos = pos_map.get(mode)
        if pos is None:
            return

        size_px   = fontsize * self.font_size / 25.0
        height_px = size_px
        width_px  = CalculateLength(text) * size_px

        timeline = time.time() - self._start_time
        with self.lock:
            self.danmaku_list.append(
                (timeline, None, None, text, pos, color, size_px, height_px, width_px)
            )

    def _parse_binary(self, data):
        pos = 0
        while pos + 16 <= len(data):
            tl = struct.unpack_from('>I', data, pos)[0]
            hl = struct.unpack_from('>H', data, pos + 4)[0]
            pv = struct.unpack_from('>H', data, pos + 6)[0]
            op = struct.unpack_from('>I', data, pos + 8)[0]
            if tl < 16 or pos + tl > len(data):
                break
            body = data[pos + hl:pos + tl]
            pos += tl
            if pv == 2:
                try:
                    self._parse_binary(zlib.decompress(body))
                except zlib.error:
                    pass
                continue
            if op == 8:
                xbmc.log('[live.danmaku] auth OK (op=8)', xbmc.LOGDEBUG)
                self._connected = True
            elif op == 5:
                try:
                    self._handle_message(body)
                except Exception:
                    pass

    def _recv_loop(self):
        buf = b''
        if self.sock:
            self.sock.settimeout(1.0)
        while self.running:
            sock = self.sock
            if not sock:
                break
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 2:
                    opcode = buf[0] & 0x0F
                    masked = (buf[1] & 0x80) != 0
                    plen   = buf[1] & 0x7F
                    offset = 2
                    if plen == 126:
                        if len(buf) < 4: break
                        plen = struct.unpack_from('>H', buf, offset)[0]
                        offset += 2
                    elif plen == 127:
                        if len(buf) < 10: break
                        plen = struct.unpack_from('>Q', buf, offset)[0]
                        offset += 8
                    if opcode == 0x8:
                        self.running = False
                        break
                    if masked:
                        if len(buf) < offset + 4 + plen: break
                        mk = buf[offset:offset + 4]
                        offset += 4
                        payload = bytes(buf[offset + i] ^ mk[i % 4] for i in range(plen))
                    else:
                        if len(buf) < offset + plen: break
                        payload = buf[offset:offset + plen]
                    offset += plen
                    buf = buf[offset:]
                    if opcode == 0x2:
                        self._parse_binary(payload)
                    elif opcode == 0x9:
                        try:
                            sock.send(bytes([0x8A, 0x00]))
                        except Exception:
                            pass
            except socket.timeout:
                continue
            except (OSError, ConnectionError, ConnectionResetError):
                break
            except Exception as e:
                xbmc.log('[live.danmaku] recv: %s' % str(e), xbmc.LOGWARNING)
                break

    def _run(self):
        info = self._get_token_wbi()
        if not info:
            xbmc.log('[live.danmaku] getDanmuInfo FAIL', xbmc.LOGERROR)
            return

        token = info.get('token', '')
        host_list = info.get('host_list', [])
        if not host_list or not token:
            xbmc.log('[live.danmaku] no host/token', xbmc.LOGERROR)
            return

        host = host_list[-1].get('host', '')
        port = host_list[-1].get('ws_port', 2244)
        xbmc.log('[live.danmaku] using %s:%s' % (host, port), xbmc.LOGDEBUG)

        if not self._connect(host, port):
            return

        self._send_auth(token)

        def _hb():
            while self.running:
                sock = self.sock
                if not sock:
                    break
                time.sleep(30)
                if not self.running:
                    break
                sock = self.sock
                if sock:
                    try:
                        _ws_send(sock, _bili_packet(2, b'{}'))
                    except Exception:
                        pass
        threading.Thread(target=_hb, daemon=True).start()

        width  = 1920
        height = 540
        reserve_blank = int((1.0 - self.display_area) * height)

        def _writer():
            last_sync = 0
            first_sync = True
            live_marker = '/live/' + str(self.room_id)
            while self.running:
                time.sleep(3)
                if not self.running:
                    break
                with self.lock:
                    now_offset = time.time() - self._start_time
                    cutoff = now_offset - self.stay_time - 2
                    snapshot = [c for c in self.danmaku_list
                                if c[0] >= cutoff]

                if not snapshot:
                    continue

                buf = io.StringIO()
                ProcessComments(
                    snapshot, buf, width, height, reserve_blank,
                    'sans-serif', self.font_size, self._opacity,
                    self.stay_time, self.stay_time,
                    [], False, None,
                )
                content = buf.getvalue()
                buf.close()

                content = content.replace('\\move(1920,', '\\move(2120,')

                tmp = self.ass_path + '.tmp'
                try:
                    with open(tmp, 'w', encoding='utf-8-sig') as f:
                        f.write(content)
                    os.replace(tmp, self.ass_path)
                except Exception:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass

                if not self.running:
                    break
                now = time.time()
                if not first_sync and now - last_sync < 9:
                    if int(now) % 5 == 0:
                        _refresh_danmaku_lock(self.room_id)
                    continue
                first_sync = False
                last_sync = now
                try:
                    p = xbmc.Player()
                    if p.isPlaying():
                        cur = xbmc.getInfoLabel('Player.Filenameandpath') or ''
                        if live_marker in cur:
                            p.setSubtitles(self.ass_path)
                except Exception:
                    pass
        threading.Thread(target=_writer, daemon=True).start()

        self._recv_loop()

    def start(self):
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()
        for _ in range(20):
            if self._connected:
                return True
            if not self.running:
                return False
            time.sleep(0.1)
        xbmc.log(
            '[live.danmaku] start: auth not confirmed after 2 s, '
            'returning True anyway (daemon thread still running)',
            xbmc.LOGDEBUG,
        )
        return self.running

    def stop(self):
        self.running = False
        sock = self.sock
        self.sock = None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass


_instances = {}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == 'nt':
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
            )
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                return code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _acquire_danmaku_lock(room_id, timeout_s=3):
    bp = _get_temp_path()
    if not bp:
        return True
    lock_path = os.path.join(bp, 'danmaku_%s.lock' % room_id)
    now_ts = time.time()
    my_pid = os.getpid()
    deadline = now_ts + timeout_s
    while now_ts < deadline:
        if not os.path.exists(lock_path):
            try:
                with open(lock_path, 'w', encoding='utf-8') as f:
                    f.write('%d\n%.0f\n' % (my_pid, now_ts))
                return True
            except Exception:
                return True
        try:
            with open(lock_path, 'r', encoding='utf-8') as f:
                content = f.read()
            parts = content.strip().split('\n')
            other_pid = int(parts[0])
            other_ts = float(parts[1]) if len(parts) > 1 else 0
        except Exception:
            other_pid, other_ts = 0, 0
        pid_alive = _pid_alive(other_pid)
        if pid_alive and (now_ts - other_ts) < timeout_s:
            time.sleep(0.3)
            now_ts = time.time()
            continue
        try:
            with open(lock_path, 'w', encoding='utf-8') as f:
                f.write('%d\n%.0f\n' % (my_pid, now_ts))
            return True
        except Exception:
            return True
    return True


def _release_danmaku_lock(room_id):
    bp = _get_temp_path()
    if not bp:
        return
    lock_path = os.path.join(bp, 'danmaku_%s.lock' % room_id)
    try:
        with open(lock_path, 'r', encoding='utf-8') as f:
            content = f.read()
        parts = content.strip().split('\n')
        if int(parts[0]) == os.getpid():
            os.remove(lock_path)
    except Exception:
        pass


def _refresh_danmaku_lock(room_id):
    bp = _get_temp_path()
    if not bp:
        return
    lock_path = os.path.join(bp, 'danmaku_%s.lock' % room_id)
    try:
        with open(lock_path, 'w', encoding='utf-8') as f:
            f.write('%d\n%.0f\n' % (os.getpid(), time.time()))
    except Exception:
        pass


def _get_temp_path():
    try:
        from addon import get_temp_path as _g
        return _g()
    except Exception:
        return None


def _get_setting(name):
    try:
        from addon import getSetting as _g
        return _g(name)
    except Exception:
        return ''


def start_live_danmaku(room_id, uid=0, cookie=''):
    bp = _get_temp_path()
    if not bp:
        return None, None
    path = os.path.join(bp, 'live_%s.ass' % room_id)

    if not _acquire_danmaku_lock(room_id):
        xbmc.log(
            '[live.danmaku] start: another process holds the lock for '
            'room=%s, skip' % room_id, xbmc.LOGINFO,
        )
        return path, None

    buvid = ''
    try:
        from addon import get_cookie_value
        buvid = get_cookie_value('buvid3')
    except Exception:
        pass

    ph = (
        '\ufeff'
        '[Script Info]\n'
        '; Script generated by plugin.video.bili live danmaku\n'
        'ScriptType: v4.00+\n'
        'PlayResX: 1920\nPlayResY: 540\n'
        'Aspect Ratio: 1920:540\nCollisions: Normal\nWrapStyle: 2\n'
        'ScaledBorderAndShadow: yes\nYCbCr Matrix: TV.601\n\n'
        '[V4+ Styles]\n'
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, '
        'OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, '
        'ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, '
        'Alignment, MarginL, MarginR, MarginV, Encoding\n'
        'Style: R2L,sans-serif,25,&H00FFFFFF,&H00FFFFFF,&H00000000,'
        '&H00000000,0,0,0,0,100,100,0.00,0.00,1,1,0,7,0,0,0,0\n\n'
        '[Events]\n'
        'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
    )
    try:
        with open(path, 'w', encoding='utf-8-sig') as f:
            f.write(ph)
    except Exception:
        pass

    key = str(room_id)
    if key in _instances:
        try:
            _instances[key].stop()
        except Exception:
            pass

    c = LiveDanmakuClient(
        room_id, path, uid, cookie,
        float(_get_setting('font_size') or 25),
        float(_get_setting('opacity') or 1.0),
        float(_get_setting('danmaku_stay_time') or 8),
        float(_get_setting('display_area') or 1.0),
        buvid,
    )
    ok = c.start()
    _instances[key] = c
    if not ok:
        xbmc.log('[live.danmaku] start FAIL for room=%s' % room_id, xbmc.LOGWARNING)
    return path, c


def stop_all_live_danmaku():
    for key in list(_instances.keys()):
        c = _instances.pop(key, None)
        if c is not None:
            try:
                c.stop()
            except Exception:
                pass
        _release_danmaku_lock(key)
