"""image/tmux.conf の結合テスト (Linux + tmux のみ)。

Windows Terminal や Claude のデスクトップアプリの端末 (どちらも Windows の ConPTY の上で動く) は、代替画面に
切り替えた直後に端末の種類の問い合わせ (Device Attributes) を受けると、応答を 2 回返すことがある。tmux は
アタッチのたびに代替画面へ切り替えてすぐ問い合わせるので、2 回目の応答がキー入力としてシェルに流れ、
mvm ssh で入るとプロンプトに `61;4;6;7;...c0;10;1c` が入った (2026-11-21、Windows Terminal で 6 回中 5 回)。
tmux に代替画面を使わせなければ起きない (同じ条件で 8 回中 0 回)。疑似端末からアタッチし、tmux が端末に
書くバイト列に代替画面への切り替えが無いことを確かめる。
"""

import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

if sys.platform != "win32":  # Windows にはない (テストの収集で落ちないように)
    import fcntl
    import pty
    import termios

ROOT = Path(__file__).resolve().parent.parent
CONF = ROOT / "image" / "tmux.conf"
ALT_SCREEN_ON = b"\x1b[?1049h"
ALT_SCREEN_OFF = b"\x1b[?1049l"


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("tmux"), "Linux と tmux が必要")
class TmuxAltScreenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock = str(Path(self.tmp.name) / "sock")
        # Windows の OpenSSH も Mac の端末も、この TERM を VM に送る
        self.env = dict(os.environ, TERM="xterm-256color", LANG="C.UTF-8")
        self.env.pop("TMUX", None)
        self.client_pid = None
        self.fd = None
        self.output = bytearray()

    def tearDown(self):
        subprocess.run(["tmux", "-S", self.sock, "kill-server"], env=self.env, capture_output=True)
        if self.client_pid:
            try:
                os.kill(self.client_pid, signal.SIGKILL)
                os.waitpid(self.client_pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.tmp.cleanup()

    def tmux(self, *args):
        return subprocess.run(["tmux", "-S", self.sock, *args], env=self.env, check=True,
                              capture_output=True, text=True).stdout

    def _drain(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            self.output += data

    def attach_and_detach(self, conf):
        """tmux のサーバーを conf で起動し、疑似端末からアタッチして抜けるまでに、tmux が端末に書いたバイト列を返す。"""
        self.tmux("-f", str(conf) if conf else "/dev/null", "new-session", "-d", "-s", "main", "-x", "80", "-y", "24")
        pid, fd = pty.fork()
        if pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            os.execvpe("tmux", ["tmux", "-S", self.sock, "-u", "attach", "-t", "main"], self.env)
        self.client_pid, self.fd = pid, fd
        threading.Thread(target=self._drain, daemon=True).start()
        deadline = time.time() + 10
        while not self.tmux("list-clients").strip():
            if time.time() > deadline:
                self.fail("tmux にアタッチできなかった")
            time.sleep(0.05)
        time.sleep(0.5)
        self.tmux("detach-client", "-s", "main")
        os.waitpid(self.client_pid, 0)
        self.client_pid = None
        time.sleep(0.2)
        return bytes(self.output)

    def test_default_tmux_uses_alt_screen(self):
        # 前提の確認: 設定なしの tmux はアタッチで代替画面に切り替え、抜けるときに戻す
        out = self.attach_and_detach(conf=None)
        self.assertIn(ALT_SCREEN_ON, out)
        self.assertIn(ALT_SCREEN_OFF, out)

    def test_image_conf_does_not_switch_to_alt_screen(self):
        out = self.attach_and_detach(conf=CONF)
        self.assertNotIn(ALT_SCREEN_ON, out, "tmux が代替画面に切り替えている (Windows の端末で応答が重複して漏れる)")
        self.assertIn(b"[detached", out)  # アタッチして抜けるまで動いたこと


if __name__ == "__main__":
    unittest.main()
