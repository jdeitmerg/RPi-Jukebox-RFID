import logging
import jukebox.plugs as plugs
import jukebox.cfghandler
from .led_strip_manager import LedStripManager
import time

logger = logging.getLogger('jb.led_strip')
cfg = jukebox.cfghandler.get_handler('jukebox')

led_strip_manager = None


@plugs.initialize
def initialize():
    global led_strip_manager
    logger.info("Initializing LED Strip Plugin (Proxy Mode)")

    enabled = cfg.setndefault('led_strip', 'enable', value=False)
    if not enabled:
        logger.info("LED Strip Plugin is disabled")
        return

    port = cfg.setndefault('led_strip', 'daemon_port', value=5559)
    kid_color = cfg.setndefault('led_strip', 'kid_color', value=[255, 255, 255])

    try:
        led_strip_manager = LedStripManager(port, tuple(kid_color))
        led_strip_manager.start()
    except Exception as e:
        logger.error(f"Failed to start LedStripManager: {e}")

    if led_strip_manager:
        # Register the control instance
        plugs.register(led_strip_manager, name='ctrl')


@plugs.atexit
def atexit(signal_id: int, **ignored_kwargs):
    global led_strip_manager
    if led_strip_manager:
        logger.info("Stopping LED Strip Plugin")
        # Trigger shutdown animation if possible
        led_strip_manager.trigger_shutdown()
        time.sleep(2.0)  # Wait for animation
        led_strip_manager.stop()
        led_strip_manager.join()


@plugs.register
def show_battery():
    """Manually trigger battery level display."""
    global led_strip_manager
    if led_strip_manager:
        status = plugs.call_ignore_errors('battmon', 'get_batt_status')
        if status:
            led_strip_manager._trigger_overlay('battery', status.get('soc', 0), duration=5)


@plugs.register
def toggle_night_mode():
    """Toggle night mode manually."""
    # TODO: Implement night mode logic
    logger.info("Toggle Night Mode (not implemented)")
