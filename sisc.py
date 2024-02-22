import socket
import sys

import gi
import platformdirs
import soco
from aiohttp import web
from textual import on, work
from textual.app import App
from textual.binding import Binding
from textual.containers import Center, Container, Horizontal, Vertical
from textual.widgets import DataTable, Static

gi.require_version("Tracker", "3.0")
from gi.repository import Tracker  # noqa: E402

SISC_PORT = 8080


def this_ip():
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
        Binding("k", "cursor_up", "Cursor Up"),
        Binding("j", "cursor_down", "Cursor Down"),
    ]


class ControllerApp(App):
    CSS = """
    LoadingIndicator {
        background: black 0%;
    }

    AlbumList {
        width: 60%;
        overflow-x: hidden;
        scrollbar-size-vertical: 1;
    }

    #now-playing {
        width: 80%;
        margin-top: 2;
        padding-top: 1;
        content-align: center middle;
        border: vkey $accent;
    }

    #sonos-player {
        height: 1;
        text-align: right;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(self):
        self.music_dir = platformdirs.user_music_dir()
        self.controller_ip = this_ip()
        super().__init__()

    def compose(self):
        with Horizontal():
            yield AlbumList(cursor_type="row")

            with Vertical():
                yield Center(Static(id="now-playing"))
                yield Container()
                yield Static(id="sonos-player")

    def on_mount(self):
        self.query_one("#now-playing").border_title = "Now Playing"

        self.query_one(AlbumList).loading = True
        self.query_one("#sonos-player").loading = True

        self.load_data()
        self.find_sonos()
        self.spawn_http("0.0.0.0", SISC_PORT)

    @work(thread=True)
    def load_data(self):
        self.library = {}
        records = fetch_music()
        for artist, album, _, location in sorted(records):
            if (artist, album) not in self.library:
                self.library[artist, album] = []

            assert location.startswith("file://")
            trimmed_path = location.removeprefix(f"file://{self.music_dir}")

            self.library[artist, album].append(trimmed_path)

        table = self.query_one(AlbumList)
        table.add_columns("Artist", "Album")
        table.add_rows(sorted(self.library.keys()))

        table.loading = False
        table.focus()

    @work(thread=True)
    def find_sonos(self):
        self.sonos, *_ = soco.discover()

        self.query_one("#sonos-player").update(f"Sonos: {self.sonos.player_name}")
        self.query_one("#sonos-player").loading = False

    @work
    async def spawn_http(self, host, port):
        app = web.Application()
        app.add_routes([web.static("/", self.music_dir)])

        self.http_runner = web.AppRunner(app)
        await self.http_runner.setup()
        site = web.TCPSite(self.http_runner, host, port)
        await site.start()

    @on(DataTable.RowSelected)
    def select_album(self, event):
        # clear previous queue
        self.sonos.clear_queue()

        # fetch tracks to dump in queue
        artist, album = event.control.get_row(event.row_key)
        for location in self.library[artist, album]:
            self.sonos.add_uri_to_queue(
                f"http://{self.controller_ip}:{SISC_PORT}{location}"
            )

        # start playing
        self.sonos.play_from_queue(0)

        # update UI
        now_playing = self.query_one("#now-playing")
        now_playing.update(f"{album},\nby {artist}")

    async def on_unmount(self):
        try:
            self.sonos.stop()
            self.sonos.clear_queue()

            await self.http_runner.cleanup()
        except AttributeError:
            pass


def main():
    app = ControllerApp()
    app.run()
    return app.return_code


if __name__ == "__main__":
    sys.exit(main())
