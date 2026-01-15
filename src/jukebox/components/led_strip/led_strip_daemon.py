#!/usr/bin/env python3
import time
import zmq
import json
import logging
import threading
import argparse
from rpi_ws281x import Color, PixelStrip

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('led_daemon')


class LedDaemon:
    def __init__(self, num_leds, pin, brightness=50):
        self.num_leds = num_leds
        self.strip = PixelStrip(num_leds, pin, brightness=brightness)
        self.strip.begin()

        self.cur_animation = None
        self.anim_start_time = 0
        self.anim_data = {}
        self.base_color = Color(255, 255, 255)  # Default white

        self._keep_running = True
        self.lock = threading.Lock()

    def set_solid(self, r, g, b):
        with self.lock:
            self.cur_animation = None
            color = Color(r, g, b)
            for i in range(self.num_leds):
                self.strip.setPixelColor(i, color)
            self.strip.show()
            logger.debug(f"Set solid color: ({r}, {g}, {b})")

    def set_bar(self, percentage, r, g, b):
        with self.lock:
            self.cur_animation = None
            num_lit = int((percentage / 100.0) * self.num_leds)
            color = Color(r, g, b)
            for i in range(self.num_leds):
                self.strip.setPixelColor(i, color if i < num_lit else Color(0, 0, 0))
            self.strip.show()
            logger.debug(f"Set bar: {percentage}%")

    def start_animation(self, name, data=None):
        with self.lock:
            self.cur_animation = name
            self.anim_start_time = time.time()
            self.anim_data = data or {}
            logger.info(f"Started animation: {name}")

    def update_animation(self):
        with self.lock:
            if not self.cur_animation:
                return

            elapsed = time.time() - self.anim_start_time

            if self.cur_animation == 'ready':
                self._pattern_ready(elapsed)
            elif self.cur_animation == 'sync':
                self._pattern_sync(elapsed)
            elif self.cur_animation == 'shutdown':
                self._pattern_shutdown(elapsed)
            elif self.cur_animation == 'charging':
                self._pattern_charging(elapsed, self.anim_data.get('soc', 0))

            self.strip.show()

    def _pattern_ready(self, elapsed):
        center = self.num_leds / 2.0
        if elapsed < 1.5:
            progress = elapsed / 1.5
            for i in range(self.num_leds):
                dist = abs(i + 0.5 - center)
                if dist < progress * (self.num_leds / 2.0):
                    self.strip.setPixelColor(i, self.base_color)
                else:
                    self.strip.setPixelColor(i, Color(0, 0, 0))
        else:
            sub_elapsed = (elapsed - 1.5) % 0.7
            brightness = abs(0.35 - sub_elapsed) / 0.35
            r = int(self.base_color.r * brightness)
            g = int(self.base_color.g * brightness)
            b = int(self.base_color.b * brightness)
            for i in range(self.num_leds):
                self.strip.setPixelColor(i, Color(r, g, b))

    def _pattern_sync(self, elapsed):
        for i in range(self.num_leds):
            self.strip.setPixelColor(i, Color(0, 0, 0))
        period = 2.0
        progress = (elapsed % period) / period
        pos = abs(0.5 - progress) * 2.0
        idx1 = int(pos * (self.num_leds / 2.0 - 0.5))
        idx2 = self.num_leds - 1 - idx1
        self.strip.setPixelColor(idx1, self.base_color)
        self.strip.setPixelColor(idx2, self.base_color)

    def _pattern_charging(self, elapsed, soc):
        period = 3.0
        progress = (elapsed % period) / period
        target_num = int((soc / 100.0) * self.num_leds)
        current_num = int(progress * self.num_leds)

        for i in range(self.num_leds):
            idx = self.num_leds - 1 - i
            if i < current_num and i < target_num:
                if i < self.num_leds * 0.2:
                    color = Color(255, 0, 0)
                elif i < self.num_leds * 0.4:
                    color = Color(255, 255, 0)
                else:
                    color = Color(0, 255, 0)
                self.strip.setPixelColor(idx, color)
            else:
                # Dim background
                # TODO: Multiply Color() directly?
                r = int(((self.base_color >> 16) & 0xFF) * 0.1)
                g = int(((self.base_color >> 8) & 0xFF) * 0.1)
                b = int((self.base_color & 0xFF) * 0.1)
                self.strip.setPixelColor(idx, Color(r, g, b))

    def _pattern_shutdown(self, elapsed):
        duration = 2.0
        progress = min(elapsed / duration, 1.0)
        center = self.num_leds / 2.0
        remaining = (1.0 - progress) * (self.num_leds / 2.0)
        for i in range(self.num_leds):
            dist = abs(i + 0.5 - center)
            if dist <= max(remaining, 0.5):
                self.strip.setPixelColor(i, self.base_color)
            else:
                self.strip.setPixelColor(i, Color(0, 0, 0))

    def clear(self):
        with self.lock:
            self.cur_animation = None
            for i in range(self.num_leds):
                self.strip.setPixelColor(i, Color(0, 0, 0))
            self.strip.show()


class MsgHandler():
    def __init__(self, led_daemon: LedDaemon, listen_port: int):
        self.led_daemon = led_daemon
        self.list_port = listen_port

    def __enter__(self):
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.REP)
        self.socket.bind(f"tcp://127.0.0.1:{self.listen_port}")
        self.socket.setsockopt(zmq.RCVTIMEO, 20)  # 20ms timeout for non-blocking receive

    def __exit__(self, exc_type, exc_value, traceback):
        self.socket.close()
        self.context.term()

    def handle_message(self):
        try:
            message = self.socket.recv_string()
            request = json.loads(message)
            method = request.get('method')
            params = request.get('params', {})

            if method == 'set_solid':
                self.daemon.set_solid(params.get('r', 0), params.get('g', 0), params.get('b', 0))
            elif method == 'set_bar':
                self.daemon.set_bar(params.get('percentage', 0),
                                params.get('r', 255),
                                params.get('g', 255),
                                params.get('b', 255))
            elif method == 'start_animation':
                self.daemon.start_animation(params.get('name'), params.get('data'))
            elif method == 'stop_animation':
                self.daemon.cur_animation = None
            elif method == 'set_base_color':
                self.daemon.base_color = Color(params.get('r', 255), params.get('g', 255), params.get('b', 255))
            elif method == 'clear':
                self.daemon.clear()

            self.socket.send_string(json.dumps({'status': 'ok'}))
        except zmq.Again:
            pass  # No request received
        self.daemon.update_animation()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--leds', type=int, default=16)
    parser.add_argument('--pin', type=int, default=12)
    parser.add_argument('--brightness', type=int, default=50)
    parser.add_argument('--port', type=int, default=5559)
    args = parser.parse_args()

    daemon = LedDaemon(args.leds, args.pin, args.brightness)

    try:
        with MsgHandler(daemon, args.port) as handler:
            logger.info(f"LED Daemon started on port {args.port} (Pin {args.pin}, {args.leds} LEDs)")
            while True:
                handler.handle_message()
                time.sleep(0.01)  # Sleep even if we just processed a request

    except KeyboardInterrupt:
        logger.info("Stopping daemon...")
    finally:
        daemon.clear()


if __name__ == "__main__":
    main()
