import socket
import sys
import xml.etree.ElementTree as ElementTree

import gi
import platformdirs
import soco
from aiohttp import web
from soco.exceptions import SoCoUPnPException
from textual import on, work
from textual.app import App
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import DataTable, Footer, Static

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


class AlbumList(DataTable):
    BINDINGS = [
        Binding("k", "cursor_up", "Cursor Up", show=False),
        Binding("j", "cursor_down", "Cursor Down", show=False),
    ]


class ControllerApp(App, inherit_bindings=False):
    CSS = """
    $primary: #feffac;
    $secondary: #45ffca;

    Screen {
      layout: horizontal;
    }

    LoadingIndicator {
      background: $background 0%;
    }

    AlbumList {
      height: 100%;
      width: 7fr;
      margin: 0 0 1 1;
      overflow-x: hidden;

      scrollbar-size-vertical: 1;

      scrollbar-background: $background-lighten-1;
      scrollbar-background-hover: $background-lighten-1;
      scrollbar-background-active: $background-lighten-1;

      scrollbar-color: $panel;
      scrollbar-color-hover: $primary;
      scrollbar-color-active: $primary;

      & > .datatable--header,
      & > .datatable--header-hover {
        text-style: bold;
        background: $background 0%;
        color: $primary;
      }

      & > .datatable--cursor {
        background: $primary
      }

      & > .datatable--hover {
        background: $boost;
      }
    }

    Vertical {
      width: 3fr;
      margin: 0 0 1 0;
      align: center middle;

      #now-playing {
        margin: 2 0 0 2;
        padding: 0 1;
        content-align: center middle;
      }
    }

    #status-bar {
      height: 1;
      layout: grid;
      grid-size: 3;
      grid-columns: 1fr 1fr 1fr;

      & > * {
        text-align: center;
      }
    }

    Footer {
      background: $background 0%;
      color: $text;
      dock: bottom;
      height: 1;

      & > .footer--highlight {
        background: $boost;
        color: $text;
      }

      & > .footer--key {
        background: $background 0%;
        color: $primary;
        text-style: none;
      }

      & > .footer--highlight-key {
        background: $boost;
        color: $secondary;
        text-style: none;
      }
    }
    """

    BINDINGS = [
        Binding("q,ctrl+c", "quit", "Quit"),
        Binding("h", "help", "Help"),
        Binding("z", "player_prev", "Prev", show=False),
        Binding("x", "player_play", "Play", show=False),
        Binding("c", "player_pause", "Pause", show=False),
        Binding("v", "player_stop", "Stop", show=False),
        Binding("b", "player_next", "Next", show=False),
        Binding("+", "adjust_volume(+5)", "Volume up", show=False),
        Binding("-", "adjust_volume(-5)", "Volume down", show=False),
    ]

    def __init__(self):
        self.library = {}
        self.music_dir = platformdirs.user_music_dir()

        self.sonos = None

        self.http_runner = None
        self.http_host = self_ip()
        self.http_port = 8080

        super().__init__()

    def compose(self):
        yield AlbumList(cursor_type="row")

        with Vertical():
            yield Static("No music selected", id="now-playing")
            yield Container()
            with Horizontal(id="status-bar"):
                yield Container()
                yield Static("Sonos [red]⬤[/red]", id="sonos-status")
                yield Static("HTTP [red]⬤[/red]", id="http-status")

        yield Footer()

    def on_mount(self):
        self.query_one(AlbumList).loading = True

        self.load_data()
        self.find_sonos()
        self.spawn_http(self.http_host, self.http_port)

    @work(thread=True)
    def load_data(self):
        records = fetch_music()
        for artist, album, _, location in sorted(records):
            t = artist, album
            if t not in self.library:
                self.library[t] = []

            assert location.startswith("file://")
            trimmed_path = location.removeprefix(f"file://{self.music_dir}")

            self.library[t].append(trimmed_path)

        table = self.query_one(AlbumList)
        table.add_columns("Album", "Artist")
        table.add_rows(reversed(t) for t in self.library.keys())

        table.loading = False
        table.focus()

    @work(thread=True)
    def find_sonos(self):
        self.sonos, *_ = soco.discover()
        self.query_one("#sonos-status").update("Sonos [#45ffca]⬤[/#45ffca]")

        self.call_from_thread(self.set_interval, 1, self.update_now_playing)

    @work
    async def spawn_http(self, host, port):
        app = web.Application()
        app.add_routes([web.static("/", self.music_dir)])

        self.http_runner = web.AppRunner(app)
        await self.http_runner.setup()
        site = web.TCPSite(self.http_runner, host, port)
        await site.start()

        self.query_one("#http-status").update("HTTP [#45ffca]⬤[/#45ffca]")

    @on(DataTable.RowSelected)
    def select_album(self, event):
        if self.sonos:
            self.add_album_to_queue_and_play(*event.control.get_row(event.row_key))

    @work(thread=True, group="playback_control", exclusive=True)
    def add_album_to_queue_and_play(self, album, artist):
        self.sonos.clear_queue()

        for location in self.library[artist, album]:
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
    def action_adjust_volume(self, v):
        self.sonos.volume = min(100, max(0, self.sonos.volume + v))

    def _parse_track_metadata(self, document):
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

    @work(thread=True)
    def update_now_playing(self):
        track_info = self.sonos.get_current_track_info()
        document = track_info.get("metadata")
        if not document:
            return

        title, album, artist = self._parse_track_metadata(document)

        display = f"[bold]{title.text}[/bold]"
        if album is not None and artist is not None:
            display += f"\n{artist.text} ⦁ {album.text}"

        np = self.query_one("#now-playing")
        if display != np.renderable:
            self.call_from_thread(np.update, display)

    async def on_unmount(self):
        if self.sonos:
            self.sonos.stop()
            self.sonos.clear_queue()

        if self.http_runner:
            await self.http_runner.cleanup()


def main():
    app = ControllerApp()
    app.run()
    return app.return_code


if __name__ == "__main__":
    sys.exit(main())
