import collections
import socket
import sys
import textwrap
import xml.etree.ElementTree as ElementTree

import gi
import platformdirs
import soco
from aiohttp import web
from soco.exceptions import SoCoUPnPException
from textual import on, work
from textual.app import App
from textual.binding import Binding
from textual.containers import Center
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Label, Static

gi.require_version("Tracker", "3.0")
from gi.repository import Tracker  # noqa: E402


def self_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0)

    try:
        s.connect(("1.1.1.1", 53))
    except (TimeoutError, InterruptedError):
        return "127.0.0.1"

    return s.getsockname()[0]


def fetch_music():
    conn = Tracker.SparqlConnection.bus_new(
        "org.freedesktop.Tracker3.Miner.Files", None, None
    )

    stmt = """
    SELECT ?artist ?album ?trackno ?url {
        ?song a nmm:MusicPiece ;
        nie:title ?title ;
        nmm:trackNumber ?trackno ;
        nmm:musicAlbum [
            nie:title ?album ;
            nmm:albumArtist [ nmm:artistName ?artist ]
        ] ;
        nie:isStoredAs ?as .
        ?as nie:url ?url .
    }
    """
    cursor = conn.query(stmt)

    records = []
    while cursor.next():
        artist, album, raw_n, url = (cursor.get_string(i)[0] for i in range(4))
        records.append((artist, album, int(raw_n), url))

    cursor.close()
    conn.close()

    return records


def parse_sonos_track_metadata(document):
    namespaces = {
        "": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
        "dc": "http://purl.org/dc/elements/1.1/",
        "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/",
        "r": "urn:schemas-rinconnetworks-com:metadata-1-0/",
    }

    root = ElementTree.fromstring(document)

    title = root.find("item/dc:title", namespaces)
    album = root.find("item/upnp:album", namespaces)
    artist = root.find("item/dc:creator", namespaces)

    return title, album, artist


class AlbumList(DataTable, inherit_bindings=False):
    BINDINGS = [
        Binding("enter", "select_cursor", "Select", show=False),
        Binding("k,up", "cursor_up", "Cursor Up", show=False),
        Binding("j,down", "cursor_down", "Cursor Down", show=False),
    ]


class HelpScreen(ModalScreen):
    CSS = """
    HelpScreen {
      align: center middle;

      & > Label {
        padding: 2 5 1 5;
      }
    }
    """

    def compose(self):
        text = textwrap.dedent(
            """\
            [#3b689f]K[/ #3b689f]   Move cursor up
            [#3b689f]J[/ #3b689f]   Move cursor down

            [#3b689f]Z[/ #3b689f]   Previous
            [#3b689f]X[/ #3b689f]   Play
            [#3b689f]C[/ #3b689f]   Pause
            [#3b689f]V[/ #3b689f]   Stop
            [#3b689f]B[/ #3b689f]   Next

            [#3b689f]+[/ #3b689f]   Volume up
            [#3b689f]-[/ #3b689f]   Volume down
            """
        )

        yield Label(text)

    def on_key(self, event):
        self._quit()

    def on_click(self):
        self._quit()

    def _quit(self):
        self.app.pop_screen()
        self.app.timer.resume()


class ControllerApp(App, inherit_bindings=False):
    CSS = """
    #now-playing {
      height: 4;
      margin: 0 2;
      border: ascii $panel-lighten-2;
      text-align: center;
    }

    Center {
      height: 1fr;
      margin: 0 2;
      border: ascii $panel-lighten-2;
    }

    LoadingIndicator {
      background: $background 0%;
    }

    AlbumList {
      height: 1fr;
      width: auto;
      overflow-x: hidden;
      scrollbar-size-vertical: 0;

      & > .datatable--header,
      & > .datatable--header-hover {
        text-style: bold;
        background: $background 0%;
        color: $secondary-lighten-3;
      }

      & > .datatable--cursor {
        background: $primary-lighten-3;
      }

      & > .datatable--hover {
        background: $boost;
      }
    }

    Footer {
      height: 1;
      dock: bottom;
      background: $background 0%;

      & > .footer--highlight {
        background: $boost;
        color: $text;
      }

      & > .footer--key {
        background: $background 0%;
        color: $primary-lighten-2;
        text-style: none;
      }

      & > .footer--highlight-key {
        background: $boost;
        color: $secondary-lighten-2;
        text-style: none;
      }
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("f1", "show_help_screen", "Help"),
        Binding("z", "player_prev", "Prev", show=False),
        Binding("x", "player_play", "Play", show=False),
        Binding("c", "player_pause", "Pause", show=False),
        Binding("v", "player_stop", "Stop", show=False),
        Binding("b", "player_next", "Next", show=False),
        Binding("+", "player_adjust_volume(+5)", "Volume up", show=False),
        Binding("-", "player_adjust_volume(-5)", "Volume down", show=False),
    ]

    def __init__(self):
        self.music_index = collections.defaultdict(list)
        self.user_music_dir = platformdirs.user_music_dir()

        self.sonos = None
        self.timer = None

        self.http_runner = None
        self.http_host = self_ip()
        self.http_port = 8080  # TODO: configure through CLI args

        super().__init__()

    def compose(self):
        yield Static("No music selected", id="now-playing")
        yield Center(AlbumList(cursor_type="row"))
        yield Footer()

    def on_mount(self):
        self.query_one(AlbumList).loading = True

        self.load_music_library()
        self.find_sonos()
        self.spawn_http(self.http_host, self.http_port)

    @work(thread=True)
    def load_music_library(self):
        records = fetch_music()
        for artist, album, _, location in sorted(records):
            assert location.startswith("file://")
            trimmed_path = location.removeprefix(f"file://{self.user_music_dir}")

            self.music_index[artist, album].append(trimmed_path)

        table = self.query_one(AlbumList)
        table.add_columns("Artist", "Album")
        table.add_rows(self.music_index.keys())

        table.loading = False
        table.focus()

    @work(thread=True)
    def find_sonos(self):
        self.sonos, *_ = soco.discover()

        def create_timer():
            self.timer = self.set_interval(1, self.update_now_playing)

        self.call_from_thread(create_timer)

    @work
    async def spawn_http(self, host, port):
        app = web.Application()
        app.add_routes([web.static("/", self.user_music_dir)])

        self.http_runner = web.AppRunner(app)
        await self.http_runner.setup()

        site = web.TCPSite(self.http_runner, host, port)
        try:
            await site.start()
        except OSError as err:
            if err.errno == 98:
                self.exit(
                    f"could not bind to port {self.http_port} (already in use)",
                    return_code=1,
                )
            else:
                self.exit(err, return_code=1)

    @on(DataTable.RowSelected)
    def select_album(self, event):
        self.add_album_to_queue_and_play(*event.control.get_row(event.row_key))

    @work(thread=True, group="playback_control", exclusive=True)
    def add_album_to_queue_and_play(self, artist, album):
        try:
            self.sonos.clear_queue()
        except AttributeError:
            return

        for location in self.music_index[artist, album]:
            uri = f"http://{self.http_host}:{self.http_port}{location}"
            self.sonos.add_uri_to_queue(uri)

        self.sonos.play()

    @work(thread=True, group="playback_control", exclusive=True)
    def action_player_prev(self):
        try:
            track_info = self.sonos.get_current_track_info()
        except AttributeError:
            return

        position = f"0{track_info['position']}"[-8:]

        # jump to previous track if at beginning, else seek current track to start
        try:
            if position < "00:00:04":
                self.sonos.previous()
            else:
                self.sonos.seek("00:00:00")
        except SoCoUPnPException:
            pass

    @work(thread=True, group="playback_control", exclusive=True)
    def action_player_play(self):
        try:
            self.sonos.play()
        except (AttributeError, SoCoUPnPException):
            pass

    @work(thread=True, group="playback_control", exclusive=True)
    def action_player_pause(self):
        try:
            self.sonos.pause()
        except (AttributeError, SoCoUPnPException):
            pass

    @work(thread=True, group="playback_control", exclusive=True)
    def action_player_stop(self):
        try:
            self.sonos.stop()
        except AttributeError:
            pass

    @work(thread=True, group="playback_control", exclusive=True)
    def action_player_next(self):
        try:
            self.sonos.next()
        except (AttributeError, SoCoUPnPException):
            pass

    @work(thread=True, group="playback_control", exclusive=True)
    def action_player_adjust_volume(self, change):
        try:
            self.sonos.volume = min(100, max(0, self.sonos.volume + change))
        except AttributeError:
            pass

    @work(thread=True)
    def update_now_playing(self):
        track_info = self.sonos.get_current_track_info()
        document = track_info.get("metadata")
        if not document:
            return

        try:
            title, album, artist = parse_sonos_track_metadata(document)
        except ElementTree.ParseError:
            return

        display = f"[bold]{title.text}[/bold]"
        if album is not None and artist is not None:
            display += f"\n{artist.text} ⦁ {album.text}"

        now_playing = self.query_one("#now-playing")
        if display != now_playing.renderable:
            self.call_from_thread(now_playing.update, display)

    def action_show_help_screen(self):
        if self.query_one(AlbumList).loading:
            return

        self.timer.pause()
        self.push_screen(HelpScreen())

    async def on_unmount(self):
        try:
            self.sonos.stop()
            self.sonos.clear_queue()
        except AttributeError:
            pass

        try:
            await self.http_runner.cleanup()
        except AttributeError:
            pass


def main():
    app = ControllerApp()
    if (err := app.run()) is not None:
        print(f"songbird: {err}", file=sys.stderr)

    return app.return_code


if __name__ == "__main__":
    sys.exit(main())
