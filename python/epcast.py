#!/usr/bin/env python3
"""Stream a video file (or test pattern) to an Epson projector over EasyMP (ports 3620/3621).

Standalone Python port of LibreMP's protocol (../core/src/protocol.rs). Needs ffmpeg and Pillow.
Video: RFB-style FramebufferUpdate, Tight/JPEG tiles of <=624x416, after a 46-byte display-config block.
Audio: s16le 22050 Hz mono PCM on the aux channel, 0xC9 + LE u32 size header per chunk.

  python3 epcast.py --ip 192.168.1.50 movie.mkv [--ss 00:10:00] [--size 1920x1080] [--adelay 0.3]
  python3 epcast.py --ip 192.168.1.50 test      # test pattern + 440 Hz tone
  EPSON_PROJECTOR_IP=192.168.1.50 python3 epcast.py movie.mkv
"""
import argparse, io, os, socket, struct, subprocess, sys, threading, time
from PIL import Image

W, H = 1024, 768  # overridden by --size
BYTES_PER_PX = 76465 / (1024 * 768)  # JPEG budget from the Windows client's full-frame tiles

def meta_block(w, h):
    """RFB-style display config: frame size, pixel format (32bpp RGB888), source size, image rect."""
    pf = bytes.fromhex("20200001ff00ff00ff00100800000000")  # byte-for-byte as the Windows client sends it
    return b"\xcc\0\0\0" + struct.pack(">HH", w, h) + pf + struct.pack(">HHHHHH", w, h, 0, 0, w, h) + b"\0" * 10

def tight_jpeg(jpeg):
    """RFB Tight encoding: control byte 0x90 (JPEG) + compact length + data.
    The Windows client always uses the 3-byte length form, so we do too."""
    n = len(jpeg)
    return bytes([0x90, n & 0x7F | 0x80, n >> 7 & 0x7F | 0x80, n >> 14 & 0xFF]) + jpeg

def tile_grid(w, h):
    """Tiles of at most 624x416, like the Windows client."""
    return [(x, y, min(624, w - x), min(416, h - y)) for y in range(0, h, 416) for x in range(0, w, 624)]

def eemp(my, cmd, payload=b""):
    return b"EEMP0100" + my + struct.pack("<II", cmd, len(payload)) + payload

def msgs(data):
    out, off = [], 0
    while off + 20 <= len(data) and data[off:off + 8] == b"EEMP0100":
        cmd, ln = struct.unpack_from("<II", data, off + 12)
        out.append((cmd, data[off + 20:off + 20 + ln])); off += 20 + ln
    return out

def recv(s, t):
    s.settimeout(t)
    try: return s.recv(65536)
    except (socket.timeout, BlockingIOError): return b""

def open_(ip, port):
    s = socket.create_connection((ip, port), timeout=3)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    return s

def auth_payload(my, proj, mac, name):
    p = bytes.fromhex("0101000000380f000000000000ffffff0000000000020f0b0004000320200001ff00ff00ff00000810000000010e0000")
    p += mac + b"\0" * 16 + proj
    p += bytes.fromhex("a600000005000000380000000200000004000000") + my
    p += bytes.fromhex("0c0000000400000000000000010000000400000050004300")
    p += bytes.fromhex("0b0000000400000000000000")
    p += bytes.fromhex("1c00000000000000040000003600000001000000030000002a000000")
    p += mac + proj + name[:32].ljust(32, b"\0")
    p += bytes.fromhex("0f00000004000000320000000d000000040000000200000026000000080000000010000000100000")
    return eemp(my, 0x0101, p)

def resp_0108(my):
    h = ("45454d5030313030c0a858020801000048010000"
         "0001000000000000000000000000000000000000"
         "00000000000000000000000000000000000000000000000000000000"
         "1401000005000000380000000200000004000000"
         "c0a858020c00000004000000010000000100000004000000"
         "500043000b00000004000000000000001c00000000000000"
         "07000000440000000100000005000000380000000200000004000000"
         "c0a858020c00000004000000010000000100000004000000"
         "500043000b00000004000000000000001c00000000000000"
         "08000000800000000400000005000000380000000200000004000000"
         "c0a858020c00000004000000010000000100000004000000"
         "500043000b00000004000000010100001c00000000000000"
         "000000000c000000020000000400000002000000"
         "000000000c000000020000000400000003000000"
         "000000000c000000020000000400000004000000")
    return bytes.fromhex(h).replace(bytes([192, 168, 88, 2]), my)

def chan_init(my, ch):
    return b"EPRD0600" + my + bytes.fromhex("0000000010000000d0000000") + my[::-1] + bytes([ch]) + b"\0" * 7

class Client:
    def __init__(self, ip):
        self.ip = ip
        s = open_(ip, 3620)
        self.my = socket.inet_aton(s.getsockname()[0]); proj = socket.inet_aton(ip)
        s.sendall(eemp(self.my, 2, bytes.fromhex("007f0000b0f8ef5314000000") + b"\0" * 36))
        reg = recv(s, 5) + recv(s, 1); s.close()
        name, mac = b"", None
        for cmd, p in msgs(reg):
            if cmd == 3 and len(p) >= 36:
                name = p[4:36].split(b"\0")[0]
                if len(p) >= 54 and any(p[48:54]): mac = mac or p[48:54]
            if cmd == 0x15 and len(p) >= 6 and any(p[:6]): mac = p[:6]
        if not mac: sys.exit("projector did not report its MAC")
        print(f"[*] projector {name.decode(errors='replace')!r} mac {mac.hex()}", file=sys.stderr)
        time.sleep(0.1)
        self.auth = open_(ip, 3620)
        self.auth.sendall(auth_payload(self.my, proj, mac, name))
        for cmd, p in msgs(recv(self.auth, 5)):
            if cmd == 0x102 and len(p) > 30 and p[30] != 0:
                sys.exit("projector refused login (keyword on, or someone else presenting)")
        responded = False
        for _ in range(10):
            d = recv(self.auth, 3)
            if not d: break
            cmds = [c for c, _ in msgs(d)]
            if 0x10E in cmds and not responded:
                self.auth.sendall(resp_0108(self.my)); responded = True
            if 0x110 in cmds: break
        time.sleep(0.3)
        self.video = open_(ip, 3621); self.video.sendall(chan_init(self.my, 0))
        self.aux = open_(ip, 3621); self.aux.sendall(chan_init(self.my, 1))
        recv(self.auth, 10)  # 0x0016
        for n in (7276, 2646, 1764):
            self.aux_send(b"\0" * n); time.sleep(0.05)
        time.sleep(0.5)
        self.last_hb = time.monotonic()
        print("[+] connected, streaming", file=sys.stderr)

    def aux_send(self, pcm):
        self.aux.sendall(b"\xc9" + struct.pack("<I", len(pcm)) + pcm)

    def upkeep(self):
        self.auth.setblocking(False)
        try: d = self.auth.recv(4096)
        except BlockingIOError: d = None
        finally: self.auth.setblocking(True)
        if d == b"": raise ConnectionError("projector closed the control channel (another device may have started presenting)")
        # answer status queries, and re-send unprompted every 30 s or the projector drops us after ~1 min
        if (d and any(c == 0x10E for c, _ in msgs(d))) or time.monotonic() - self.last_hb > 30:
            self.auth.sendall(resp_0108(self.my)); self.last_hb = time.monotonic()

    def frame(self, rgb, q):
        img = Image.frombytes("RGB", (W, H), rgb)
        body = struct.pack(">BxH", 0, len(TILES))  # RFB FramebufferUpdate header
        for x, y, w, h in TILES:
            tile, tq, budget = img.crop((x, y, x + w, y + h)), q, max(2048, int(w * h * BYTES_PER_PX * self.scale))
            while True:
                b = io.BytesIO(); tile.save(b, "JPEG", quality=tq, subsampling=2)
                if b.tell() <= budget or tq <= 10: break
                tq -= 10
            # RFB FramebufferUpdate rect: x, y, w, h, encoding 7 (Tight)
            body += struct.pack(">HHHHi", x, y, w, h, 7) + tight_jpeg(b.getvalue())
        meta = meta_block(W, H)
        blk = b"EPRD0600" + self.my + struct.pack("<II", 0, len(meta)) + meta
        blk += b"EPRD0600" + self.my + struct.pack(">II", 0, len(body)) + body
        self.video.sendall(blk)

    def bye(self):
        try: self.auth.sendall(eemp(self.my, 0x104)); recv(self.auth, 1)
        except OSError: pass

def has_audio(src):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", src], capture_output=True, text=True)
    return bool(r.stdout.strip())

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="video file/URL, or 'test' for test pattern + 440Hz tone")
    ap.add_argument("--ip", default=os.environ.get("EPSON_PROJECTOR_IP"),
                    help="projector address (default: $EPSON_PROJECTOR_IP)")
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--size", default="1280x720", help="stream resolution WxH (1024x768 and 1280x720 verified)")
    ap.add_argument("--quality", type=int, default=85, help="starting JPEG quality (stepped down to fit budget)")
    ap.add_argument("--bitrate-scale", type=float, default=1.5, help="multiplier on per-tile JPEG byte budget")
    ap.add_argument("--ss", help="start offset, e.g. 00:10:00")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--adelay", type=float, default=0.0, help="delay audio by N seconds to line up with video")
    a = ap.parse_args()
    if not a.ip:
        ap.error("give the projector address with --ip or $EPSON_PROJECTOR_IP")
    global W, H, TILES
    W, H = map(int, a.size.lower().split("x"))
    TILES = tile_grid(W, H)

    # one ffmpeg for both streams so they share a clock: video -> stdout, audio -> a pipe fd
    if a.src == "test":
        inp = ["-re", "-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate={a.fps}",
               "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050"]
        amap, audio = "1:a:0", not a.no_audio
    else:
        inp = ["-re", *(["-ss", a.ss] if a.ss else []), "-i", a.src]
        amap, audio = "0:a:0", not a.no_audio and has_audio(a.src)
    vf = f"fps={a.fps},scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"
    cmd = ["ffmpeg", "-loglevel", "error", "-nostdin", *inp,
           "-map", "0:v:0", "-vf", vf, "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
    ar, aw = os.pipe()
    if audio:
        af = f"adelay={int(a.adelay * 1000)}:all=1" if a.adelay > 0 else "anull"
        cmd += ["-map", amap, "-af", af, "-f", "s16le", "-ar", "22050", "-ac", "1", f"pipe:{aw}"]

    c = Client(a.ip)
    c.scale = a.bitrate_scale
    ff = subprocess.Popen(cmd, stdout=subprocess.PIPE, pass_fds=(aw,))
    os.close(aw)
    stop = threading.Event()
    latest, have_frame = [None], threading.Condition()

    def audio_loop():
        # projector wants s16le 22050 Hz mono; ffmpeg -re paces the reads
        with os.fdopen(ar, "rb", buffering=0) as f:
            while audio and not stop.is_set():
                d = f.read(4410)  # 100 ms
                if not d: break
                c.aux_send(d)
        t = time.monotonic()
        while not stop.is_set():  # keepalive silence, like the Windows client
            c.aux_send(b"\0" * 2646); c.aux_send(b"\0" * 1764)
            t += 0.1; time.sleep(max(0, t - time.monotonic()))

    def read_loop():
        # keep only the newest frame so a slow encoder drops frames instead of stalling audio
        while True:
            rgb = ff.stdout.read(W * H * 3)
            with have_frame:
                latest[0] = rgb if len(rgb) == W * H * 3 else b""
                have_frame.notify()
            if not latest[0]: return

    threading.Thread(target=audio_loop, daemon=True).start()
    threading.Thread(target=read_loop, daemon=True).start()
    n, t0 = 0, time.monotonic()
    try:
        while True:
            with have_frame:
                while latest[0] is None: have_frame.wait()
                rgb, latest[0] = latest[0], None
            if not rgb: break
            c.upkeep()
            c.frame(rgb, a.quality)
            n += 1
            if n % (a.fps * 10) == 0:
                print(f"  {n} frames, {n / (time.monotonic() - t0):.1f} fps", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set(); ff.kill(); c.bye()
        print(f"[*] done, {n} frames", file=sys.stderr)

if __name__ == "__main__":
    main()
