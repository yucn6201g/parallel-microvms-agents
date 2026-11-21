"""image/tmux.conf の結合テスト (Linux + tmux のみ)。

mvm ssh でアタッチしたとき、手元の端末が返す応答 (Device Attributes など) が網越しに分かれて届くと、
tmux は escape-time のあいだに続きが来なければ解釈をあきらめ、残りをキー入力としてペインに流す。
tmux 3.5 で escape-time の既定が 500ms から 10ms になったため、MicroVM の tmux (3.6a) ではシェルの
プロンプトに `61;6;7;...c0;10;1c` が入ることがあった。疑似端末からアタッチし、応答を分けて送って確かめる。
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
# tmux 3.5 以降の既定値。CI の tmux (3.4 など) は既定が 500ms なので、明示してそろえる
NEW_TMUX_DEFAULT_MS = 10
# 応答の前半と後半の間隔。網越しの遅れを真似る
GAP_SECONDS = 0.03
# 端末が返す応答 (Device Attributes の 2 次応答)。xterm.js や Windows Terminal が返す形
DA2_RESPONSE = b"\x1b[>0;10;1c"


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("tmux"), "Linux と tmux が必要")
class TmuxSplitResponseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.sock = str(t / "sock")
        self.out = t / "pane-input"
        self.env = dict(os.environ, TERM="xterm-256color", LANG="C.UTF-8")
        self.env.pop("TMUX", None)
        self.client_pid = None
        self.fd = None

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

    def start(self, conf):
        """ペインには、受け取ったバイトをそのままファイルに書くプログラムを置き、疑似端末からアタッチする。"""
        cmd = ["-f", "/dev/null", "new-session", "-d", "-s", "t", "-x", "80", "-y", "24",
               f"stty raw -echo; exec cat > '{self.out}'",
               ";", "set-option", "-s", "escape-time", str(NEW_TMUX_DEFAULT_MS)]
        if conf:
            cmd += [";", "source-file", str(conf)]
        self.tmux(*cmd)
        pid, fd = pty.fork()
        if pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            os.execvpe("tmux", ["tmux", "-S", self.sock, "attach", "-t", "t"], self.env)
        self.client_pid, self.fd = pid, fd
        # tmux が端末に書く画面を読み捨てる (読まないと tmux が書き込みで止まる)
        threading.Thread(target=self._drain, daemon=True).start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.tmux("list-clients").strip() and self.out.exists():
                break
            time.sleep(0.05)
        else:
            self.fail("tmux にアタッチできなかった")
        time.sleep(0.5)  # アタッチ直後の tmux 自身の問い合わせが落ち着くのを待つ

    def _drain(self):
        while True:
            try:
                if not os.read(self.fd, 65536):
                    return
            except OSError:
                return

    def pane_input_after_split_response(self, conf):
        self.start(conf)
        os.write(self.fd, DA2_RESPONSE[:5])
        time.sleep(GAP_SECONDS)
        os.write(self.fd, DA2_RESPONSE[5:])
        time.sleep(0.3)
        os.write(self.fd, b"END")
        deadline = time.time() + 5
        while time.time() < deadline:
            data = self.out.read_bytes()
            if data.endswith(b"END"):
                return data
            time.sleep(0.05)
        self.fail(f"ペインに END が届かなかった: {self.out.read_bytes()!r}")

    def test_split_response_leaks_with_new_tmux_default(self):
        # 前提の確認: 設定なし (escape-time 10ms) では、分かれて届いた応答がペインに漏れる
        data = self.pane_input_after_split_response(conf=None)
        self.assertIn(b"10;1c", data)

    def test_split_response_does_not_leak_with_image_conf(self):
        data = self.pane_input_after_split_response(conf=CONF)
        self.assertEqual(data, b"END", f"端末の応答がペインに漏れた: {data!r}")


if __name__ == "__main__":
    unittest.main()
