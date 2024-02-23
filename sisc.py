import socket
import sys
import xml.etree.ElementTree as ElementTree

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
        margin-top: 1;
        content-align: center middle;
    }

    #status-bar {
        height: 1;
        layout: grid;
        grid-size: 3;
        grid-columns: 2fr 1fr 1fr;
    }

    #sonos-status, #http-status {
        text-align: center;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(self):
        self.sonos = None
        self.http_runner = None
        self.music_dir = platformdirs.user_music_dir()
        self.controller_ip = this_ip()
        super().__init__()

    def compose(self):
        with Horizontal():
            yield AlbumList(cursor_type="row")

            with Vertical():
                yield Center(Static("No music selected", id="now-playing"))
                yield Container()
                with Horizontal(id="status-bar"):
                    yield Static()
                    yield Static("Sonos [red]⬤[/red]", id="sonos-status")
                    yield Static("HTTP [red]⬤[/red]", id="http-status")

    def on_mount(self):
        self.query_one(AlbumList).loading = True

        self.load_data()
        self.find_sonos()
        self.spawn_http(self.controller_ip, SISC_PORT)

    @work(thread=True)
    def load_data(self):
        self.library = {}
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
        self.query_one("#sonos-status").update("Sonos [green]⬤[/green]")

        self.call_from_thread(self.set_interval, 1, self.update_current_track)

    @work(thread=True)
    def update_current_track(self):
        track_info = self.sonos.get_current_track_info()
        raw_document = track_info.get("metadata")
        if not raw_document:
            return

        root = ElementTree.fromstring(raw_document)

        namespaces = {
            "": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
            "dc": "http://purl.org/dc/elements/1.1/",
            "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/",
            "r": "urn:schemas-rinconnetworks-com:metadata-1-0/",
        }

        title = root.find("item/dc:title", namespaces)
        album = root.find("item/upnp:album", namespaces)
        artist = root.find("item/dc:creator", namespaces)

        display = f"[bold]{title.text}[/bold]"
        if album is not None and artist is not None:
            display += f"\n{artist.text} ⦁ {album.text}"

        np = self.query_one("#now-playing")
        if display != np.renderable:
            self.call_from_thread(np.update, display)

    @work
    async def spawn_http(self, host, port):
        app = web.Application()
        app.add_routes([web.static("/", self.music_dir)])

        self.http_runner = web.AppRunner(app)
        await self.http_runner.setup()
        site = web.TCPSite(self.http_runner, host, port)
        await site.start()

        self.query_one("#http-status").update("HTTP [green]⬤[/green]")

    @on(DataTable.RowSelected)
    def select_album(self, event):
        t = event.control.get_row(event.row_key)
        self.play_album(*t)

    @work(thread=True, group="control", exclusive=True)
    def play_album(self, album, artist):
        self.sonos.clear_queue()

        for location in self.library[artist, album]:
            uri = f"http://{self.controller_ip}:{SISC_PORT}{location}"
            self.sonos.add_uri_to_queue(uri)

        self.sonos.play_from_queue(0)

    async def on_unmount(self):
        self.sonos.stop()
        self.sonos.clear_queue()

        await self.http_runner.cleanup()


def main():
    app = ControllerApp()
    app.run()
    return app.return_code


if __name__ == "__main__":
    sys.exit(main())
