import socket
import threading
import time

GAME_PORT = 9000         # lobby/game UDP port clients JOIN on
BROADCAST_INTERVAL = 1.0
TICK_RATE = 10
DT = 1.0 / TICK_RATE

BASE_LOBBY_X = 100
BASE_LOBBY_Y = 250
SPACING_X = 80
MAX_PLAYERS = 4
PLAYER_COLORS = ["pink", "red", "yellow", "green", "blue", "purple"]

class LobbyServer:
    def __init__(self, game_port=GAME_PORT):
        self.game_port = game_port

        # Main UDP socket for lobby messages (JOIN, READY, SET_COLOR)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", self.game_port))
        self.sock.setblocking(False)

        # players: pid(str) -> { ready, color, lobby_x, lobby_y, addr }
        self.players = {}
        self.addr_to_pid = {}   # maps (ip,port) -> pid (string)d
        self.next_pid = 1
        self.last_heard = {}
        self.lobby_open = True
        self.game_started = False

        self.countdown_active = False
        self.countdown_time = 5
        self.countdown_remaining = None
        self.last_countdown_int = None

        self.running = True

        # Start broadcaster and main tick loop
        threading.Thread(target=self.discovery_broadcast_loop, daemon=True).start()

        print(f"[Server] Listening on 0.0.0.0:{self.game_port}")

    def discovery_broadcast_loop(self):
        b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        b.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    # assign lobby positions when a new player joins or leaves
    def assign_lobby_positions(self):
        pids = list(self.players.keys())
        for i, pid in enumerate(pids):
            # guard in case player removed concurrently
            if pid in self.players:
                self.players[pid]["lobby_x"] = BASE_LOBBY_X + i * SPACING_X
                self.players[pid]["lobby_y"] = BASE_LOBBY_Y

    # when a player joins, assign default values (pid, x and y, color)
    def handle_join(self, addr):
        if len(self.players) >= MAX_PLAYERS:
            try:
                self.sock.sendto(b"FULL", addr)
            except Exception:
                pass
            return

        # assign a simple increasing ID (string)
        pid = str(self.next_pid)
        self.next_pid += 1

        # choose a color not already used
        used_colors = {p["color"] for p in self.players.values()}
        chosen_color = None
        for c in PLAYER_COLORS:
            if c not in used_colors:
                chosen_color = c
                break
        if chosen_color is None:
            chosen_color = PLAYER_COLORS[len(self.players) % len(PLAYER_COLORS)]

        # store player record (addr saved here)
        self.players[pid] = {
            "ready": False,
            "color": chosen_color,
            "lobby_x": 0,
            "lobby_y": 0,
            "addr": addr
        }

        # register addr -> pid mapping
        try:
            self.addr_to_pid[addr] = pid
        except Exception:
            # mapping failure is non-fatal
            pass

        self.assign_lobby_positions()

        # reply with assigned ID
        try:
            self.sock.sendto(f"ASSIGN_ID,{pid}".encode("utf-8"), addr)
            print(f"[Server] Assigned ID {pid} to {addr}")
        except Exception as e:
            print("[Server] Failed to send ASSIGN_ID:", e)

    # no two players can have the same color
    def handle_set_color(self, pid, color):
        if pid not in self.players:
            return
        # don't allow duplicate colors
        used = {p["color"] for k, p in self.players.items() if k != pid}
        if color in used:
            return  # ignore request
        self.players[pid]["color"] = color

    # flip the ready value for the player
    def handle_ready_toggle(self, pid):
        if pid in self.players:
            self.players[pid]["ready"] = not self.players[pid]["ready"]
            print(f"[Server] Player {pid} ready toggled → {self.players[pid]['ready']}")

    # receive data from the clients
    def receive_packets(self):
        while True:
            # --- Receive phase ---
            try:
                data, addr = self.sock.recvfrom(4096)
            except BlockingIOError:
                # nothing to read right now
                return
            except OSError as e:
                # recvfrom raised at socket level; sometimes OS reports ICMP resets
                # but we have no addr to map -> ignore silently
                if getattr(e, "errno", None) == 10054:
                    # can't map to a pid because we don't have addr - ignore
                    return
                # log and return for other unexpected socket errors
                print("[Server] receive error (recvfrom):", e)
                return

            if not data:
                continue

            # --- Processing phase: we have data and addr ---
            try:
                text = data.decode("utf-8").strip()
                parts = text.split(",")
                cmd = parts[0]

                if cmd == "JOIN":
                    self.handle_join(addr)

                elif cmd == "SET_COLOR" and len(parts) >= 3:
                    pid = parts[1]
                    color = parts[2]
                    if pid in self.players:
                        self.handle_set_color(pid, color)
                        self.broadcast_lobby_state()

                elif cmd == "SET_READY" and len(parts) >= 2:
                    pid = parts[1]
                    if pid in self.players:
                        self.players[pid]["ready"] = True
                        print(f"[Server] Received SET_READY for {pid}")
                        self.broadcast_lobby_state()

                elif cmd == "SET_NOT_READY" and len(parts) >= 2:
                    pid = parts[1]
                    if pid in self.players:
                        self.players[pid]["ready"] = False
                        print(f"[Server] Received SET_NOT_READY for {pid}")
                        self.broadcast_lobby_state()

                elif cmd == "DISCONNECT" and len(parts) >= 2:
                    pid = parts[1]
                    if pid in self.players:
                        print(f"[Server] Player {pid} disconnected (DISCONNECT msg)")
                        self.remove_client(pid)

                elif cmd == "HEARTBEAT" and len(parts) >= 2:
                    pid = parts[1]
                    self.last_heard[pid] = time.time()
                # unknown commands: ignore silently

            except OSError as e:
                # Per-packet OS errors: check if it's a WSAECONNRESET (10054).
                # In that case, try to identify the offending pid by addr.
                if getattr(e, "errno", None) == 10054:
                    offender = self.addr_to_pid.get(addr)
                    if offender:
                        print(f"[Server] Client at {addr} forcibly closed. Removing pid {offender}.")
                        self.remove_client(offender)
                    # if addr not known, ignore
                    continue

                print("[Server] receive error (processing):", e)
                continue

            except Exception as e:
                # Catch-all: if it looks like a connection reset with errno, try to map it.
                if isinstance(e, OSError) and getattr(e, "errno", None) == 10054:
                    offender = self.addr_to_pid.get(addr)
                    if offender:
                        print(f"[Server] Client at {addr} reset connection — removing pid {offender}.")
                        self.remove_client(offender)
                        continue
                    else:
                        # unknown addr reset -> ignore silently
                        continue

                # otherwise log and continue
                print("[Server] receive error (ignored):", e)
                continue

    # if a client has left, remove their data, reassign lobby positions,
    def remove_client(self, pid):
        # fully remove the player from all tracking structures
        if pid in self.players:
            del self.players[pid]

        if pid in self.last_heard:
            del self.last_heard[pid]

        # remove addr mapping
        dead_addr = None
        for addr,p in list(self.addr_to_pid.items()):
            if p == pid:
                dead_addr = addr
                break
        if dead_addr:
            del self.addr_to_pid[dead_addr]

        # reassign lobby positions
        self.assign_lobby_positions()

        # reset ready states
        for p in self.players.values():
            p["ready"] = 0

        # broadcast updated state
        self.broadcast_lobby_state()

        # cancel countdown
        self.broadcast_cancel_countdown()

        print(f"[Server] Removed player {pid} and cleaned state")

    def timeout_check(self):
        now = time.time()
        for pid, last in list(self.last_heard.items()):
            if now - last > 2.25:
                print(f"[Server] Timeout: removing {pid}")
                self.remove_client(pid)

    # Gather all player data at this moment
    def format_lobby_state(self):
        chunks = ["LOBBY_STATE"]
        # iterate over a stable snapshot
        for pid, p in list(self.players.items()):
            color = p.get("color", "red")
            ready = int(bool(p.get("ready", False)))
            lx = int(p.get("lobby_x", 0))
            ly = int(p.get("lobby_y", 0))
            chunks.append(f"{pid},{color},{ready},{lx},{ly}")
        return ";".join(chunks).encode("utf-8")

    # Broadcast all current player data
    def broadcast_lobby_state(self):
        msg = self.format_lobby_state()
        for pid, p in list(self.players.items()):
            try:
                self.sock.sendto(msg, p["addr"])
            except Exception:
                # ignore send errors (client may have vanished)
                pass

    def broadcast_countdown(self):
        msg = f"COUNTDOWN,{int(self.countdown_remaining)}".encode("utf-8")
        for pid, p in list(self.players.items()):
            try:
                self.sock.sendto(msg, p["addr"])
            except Exception:
                pass

    def check_start_condition(self):
        if self.lobby_open and len(self.players) > 0:
            if all(p.get("ready", False) for p in self.players.values()):
                print("[Server] All players ready! Starting countdown...")
                self.lobby_open = False
                self.countdown_active = True
                self.countdown_remaining = self.countdown_time
                self.last_countdown_int = None

    def broadcast_cancel_countdown(self):
        msg = b"COUNTDOWN_CANCEL"
        for pid, p in list(self.players.items()):
            try:
                self.sock.sendto(msg, p["addr"])
            except Exception:
                pass

    def tick(self):
        # Process incoming packets
        self.receive_packets()
        # If countdown is running, handle countdown ticks
        if self.countdown_active:
            # Abort if any player unreadies during countdown
            if not all(p.get("ready", False) for p in self.players.values()):
                self.countdown_active = False
                self.lobby_open = True
                self.last_countdown_int = None
                print("[Server] Countdown canceled! A player un-readied.")
                # notify clients and sync lobby state
                self.broadcast_cancel_countdown()
                self.broadcast_lobby_state()
                return

            # if all players are still ready, send countdown only when integer value changes
            current_int = int(self.countdown_remaining)
            if current_int != self.last_countdown_int:
                self.last_countdown_int = current_int
                print(f"[Server] Countdown: {current_int}")
                self.broadcast_countdown()

            # decrement the countdown
            self.countdown_remaining -= DT

            if self.countdown_remaining <= 0:
                self.countdown_active = False
                self.game_started = True
                print("[Server] Countdown finished. Game starting now.")
                # note: game initialization would go here
                return

        # Normal lobby state updates
        self.broadcast_lobby_state()
        self.check_start_condition()
        self.timeout_check()

    # the main running loop schedules each tick and runs them
    def run(self):
        next_tick = time.time()
        while True:
            now = time.time()
            if now >= next_tick:
                self.tick()
                next_tick += DT
            else:
                time.sleep(0.001)

if __name__ == "__main__":
    server = LobbyServer()
    server.run()