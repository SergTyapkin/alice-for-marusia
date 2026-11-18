"""Best-effort shared sysfs LED indicator for the original Capsula."""

import math, os, threading, time


class ListeningLeds:
    def __init__(self, root="/sys/class/leds", interval=1 / 30):
        self.paths = [
            os.path.join(root, "led%d" % i, "brightness") for i in range(1, 40)
        ]
        self.interval = interval
        self.owned = False
        self.last = None
        self.updated = 0
        self.smooth = 0
        self.fds = []
        self.regular_files = not os.path.realpath(root).startswith("/sys/")

    def open_channels(self):
        try:
            for path in self.paths:
                self.fds.append(os.open(path, os.O_RDWR))
        except OSError:
            self.close_channels()
            raise

    def close_channels(self):
        for fd in self.fds:
            os.close(fd)
        self.fds = []

    def read(self):
        if not self.fds:
            self.open_channels()
        values = []
        for fd in self.fds:
            os.lseek(fd, 0, os.SEEK_SET)
            values.append(int(os.read(fd, 32)))
        return values

    def write_changes(self, current, target):
        for fd, old, new in zip(self.fds, current, target):
            if old != new:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, str(new).encode("ascii"))
                if self.regular_files:
                    os.ftruncate(fd, len(str(new)))

    def start(self):
        try:
            current = self.read()
            # Do not take over an existing light owned by the stock assistant.
            if any(current):
                self.close_channels()
                return False
            self.last = current
            self.owned = True
            self.smooth = 0
            self.updated = 0
            self.update(0, force=True)
            return self.owned
        except (OSError, ValueError):
            self.owned = False
            self.close_channels()
            return False

    def update(self, rms, force=False):
        now = time.monotonic()
        if not self.owned or (not force and now - self.updated < self.interval):
            return
        try:
            current = self.read()
            if current != self.last:
                # Yield channels changed by the stock assistant while removing
                # our unchanged contributions, rather than leaving them on.
                target = [
                    0 if value == ours and ours else value
                    for value, ours in zip(current, self.last)
                ]
                self.write_changes(current, target)
                self.owned = False
                self.last = None
                self.close_channels()
                return
            # Floor keeps the ring visible during silence; cap limits glare.
            level = min(1.0, max(0.0, (rms - 200) / 3500.0))
            self.smooth = 0.6 * self.smooth + 0.4 * math.sqrt(level)
            red = round(10 + 130 * self.smooth)
            green = round(red * 0.85)
            # The large Capsule's ring channels are G,R,B; the panel is R,G,B.
            target = [component for _ in range(12) for component in (green, red, 0)] + [
                35,
                23,
                0,
            ]
            self.write_changes(current, target)
            self.last = target
            self.updated = now
        except (OSError, ValueError):
            self.owned = False
            self.close_channels()

    def thinking(self, now):
        if not self.owned:
            return
        try:
            current = self.read()
            if current != self.last:
                target = [
                    0 if value == ours and ours else value
                    for value, ours in zip(current, self.last)
                ]
                self.write_changes(current, target)
                self.owned = False
                self.last = None
                self.close_channels()
                return
            # A dim ring with a brighter point travelling around the 12 LEDs.
            position = (now * 8.0) % 12
            target = []
            for pixel in range(12):
                distance = abs(pixel - position)
                distance = min(distance, 12 - distance)
                red = round(3 + 24 * max(0, 1 - distance / 2.0))
                green = round(red * 0.85)
                target.extend((green, red, 0))
            target.extend((6, 5, 0))
            self.write_changes(current, target)
            self.last = target
            self.updated = now
        except (OSError, ValueError):
            self.owned = False
            self.close_channels()

    def stop(self):
        if not self.owned:
            return
        try:
            current = self.read()
            target = [
                0 if value == ours and ours else value
                for value, ours in zip(current, self.last)
            ]
            self.write_changes(current, target)
        except (OSError, ValueError):
            pass
        finally:
            self.owned = False
            self.last = None
            self.close_channels()


class IndicatorWorker:
    """Keep sysfs I/O out of the streaming recognizer's audio loop."""

    def __init__(self, driver=None, interval=1 / 30):
        self.driver = driver or ListeningLeds()
        self.condition = threading.Condition()
        self.desired = False
        self.level = 0
        self.output_level = 0
        self.source = "listen"
        self.exiting = False
        self.interval = interval
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def set_listening(self, active, source="listen"):
        with self.condition:
            self.desired = active
            self.source = source
            self.condition.notify()

    def update(self, rms):
        with self.condition:
            self.level = rms

    def update_output(self, rms):
        with self.condition:
            self.output_level = rms

    def run(self):
        while True:
            frame_start = time.monotonic()
            with self.condition:
                if self.exiting:
                    break
                desired = self.desired
                source = self.source
                level = self.output_level if source == "output" else self.level
            if desired:
                if not self.driver.owned:
                    self.driver.start()
                if self.driver.owned:
                    if source == "thinking":
                        self.driver.thinking(time.monotonic())
                    else:
                        self.driver.update(level, force=True)
            else:
                self.driver.stop()
            with self.condition:
                if not self.exiting:
                    self.condition.wait(
                        max(0.001, self.interval - (time.monotonic() - frame_start))
                    )
        self.driver.stop()

    def close(self):
        with self.condition:
            self.exiting = True
            self.condition.notify()
        self.thread.join(timeout=2)
