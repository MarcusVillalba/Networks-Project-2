import pygame
import socket
import threading
import time
import os

SERVER_IP = "192.168.1.177"
SERVER_PORT = 9000

# ------------------------------------------------------------
# Network client
# ------------------------------------------------------------
class LobbyClient:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.1)

        self.player_states = {}
        self.player_id = None
        self.lobby_players = []
        self.message_log = []
        self.player_color = None
        self.color_map = {}
        self.countdown_value = None
        self.last_heartbeat = time.time()
        
        join_msg = "JOIN".encode("utf-8")
        self.sock.sendto(join_msg, (SERVER_IP, SERVER_PORT))
        self.message_log.append("Sent JOIN...")

        self.running = True
        self.thread = threading.Thread(target=self.recv_loop)
        self.thread.start()

    def recv_loop(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(2048)
            except:
                continue

            text = data.decode("utf-8")

            # FORMAT: ASSIGN_ID,<id>
            if text.startswith("ASSIGN_ID"):
                self.player_id = int(text.split(",")[1])
                self.message_log.append(f"Assigned ID = {self.player_id}")

            # FORMAT: LOBBY_STATE,<id1>-<ready1>,<id2>-<ready2>,...
            elif text.startswith("LOBBY_STATE"):
                # Format: LOBBY_STATE;pid,color,ready,lx,ly;pid,color,ready,lx,ly;...

                entries = text.split(";")[1:]  # skip LOBBY_STATE prefix

                new_player_states = {}
                new_color_map = {}
                new_lobby_list = []

                for entry in entries:
                    entry = entry.strip()
                    if not entry:
                        continue

                    fields = entry.split(",")
                    if len(fields) != 5:
                        continue

                    pid_str, color_str, ready_str, lx_str, ly_str = fields

                    try:
                        pid = int(pid_str)
                    except:
                        continue

                    try:
                        ready_val = int(ready_str)
                    except:
                        ready_val = 0

                    try:
                        lx = int(lx_str)
                        ly = int(ly_str)
                    except:
                        lx, ly = 50, 50

                    # Rebuild dicts
                    new_color_map[pid] = color_str
                    new_player_states[pid] = {
                        "color": color_str,
                        "x": lx,
                        "y": ly,
                        "ready": ready_val
                    }

                    new_lobby_list.append((pid, ready_val))

                    if pid == self.player_id:
                        self.player_color = color_str

                # ---- Replace old state atomically ----
                self.color_map = new_color_map
                self.player_states = new_player_states
                self.lobby_players = new_lobby_list


            # handle countdown cancel sent from server
            if text == "COUNTDOWN_CANCEL":
                # stop showing countdown
                self.countdown_value = None
                # optional: log a message
                self.message_log.append("Countdown canceled")
                continue


            # FORMAT: COUNTDOWN,<seconds>
            elif text.startswith("COUNTDOWN"):
                t = text.split(",")[1]
                try:
                    self.countdown_value = int(t)    # <<< STORE VALUE FOR UI
                except:
                    pass
                self.message_log.append(f"Game starting in {t}")

            elif text.startswith("COUNTDOWN_CANCEL"):
                self.countdown_value = None

            else:
                self.message_log.append(f"Unknown msg: {text}")

    def send_ready(self):
        if not self.player_id:
            print("You don't have a player ID yet.")
            return
        msg = f"SET_READY,{self.player_id}".encode("utf-8")
        self.sock.sendto(msg, (SERVER_IP, SERVER_PORT))

    def send_not_ready(self):
        if not self.player_id:
            print("You don't have a player ID yet.")
            return
        msg = f"SET_NOT_READY,{self.player_id}".encode("utf-8")
        self.sock.sendto(msg, (SERVER_IP, SERVER_PORT))

    def set_color(self, new_color):
        if self.player_id:
            msg = f"SET_COLOR,{self.player_id},{new_color}".encode("utf-8")
            self.sock.sendto(msg, (SERVER_IP, SERVER_PORT))

    def close(self):
        self.running = False
        self.thread.join()
        self.sock.close()


# ------------------------------------------------------------
# Pygame UI
# ------------------------------------------------------------
pygame.init()
WIN = pygame.display.set_mode((600, 400))
pygame.display.set_caption("Lobby Frontend")
FONT = pygame.font.SysFont("Arial", 24)
SMALL = pygame.font.SysFont("Arial", 16)
AVAILABLE_COLORS = ["pink", "red", "yellow", "green", "blue", "purple"]

# Load dot images (one per color)
DOT_IMAGES = {}

color_folder = "assets/colors"
for filename in os.listdir(color_folder):
    if filename.lower().endswith(".png"):
        color_name = filename[:-4]  # "red.png" -> "red"
        DOT_IMAGES[color_name] = pygame.image.load(
            os.path.join(color_folder, filename)
        ).convert_alpha()


client = LobbyClient()

clock = pygame.time.Clock()
running = True

def cycle_color(direction, current_color, all_colors, used_colors):
    n = len(all_colors)
    i = all_colors.index(current_color)
    start = i
    while True:
        i = (i + direction) % n
        c = all_colors[i]
        if c not in used_colors:
            return c
        if i == start:
            return current_color


def draw_text(surface, text, x, y, font, color=(255,255,255)):
    img = font.render(text, True, color)
    surface.blit(img, (x, y))


while running:
    clock.tick(60)

    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False

        # Press SPACE to toggle ready
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_LEFT:
                if client.player_color is not None:
                    used = set(client.color_map.values())  # all colors currently in use
                    new_color = cycle_color(
                        -1,
                        client.player_color,
                        AVAILABLE_COLORS,
                        used - {client.player_color}  # allow keeping own color
                    )
                    client.set_color(new_color)

            if event.key == pygame.K_RIGHT:
                if client.player_color is not None:
                    used = set(client.color_map.values())
                    new_color = cycle_color(
                        1,
                        client.player_color,
                        AVAILABLE_COLORS,
                        used - {client.player_color}
                    )
                    client.set_color(new_color)

            if event.key == pygame.K_SPACE:
                # if player is ready, un-ready; else set ready
                if client.player_id is not None:
                    # determine current known state (fallback 0)
                    own = next((r for (pid, r) in client.lobby_players if pid == client.player_id), 0)

                    # optimistic toggle locally so UI updates immediately
                    new_local = 0 if own == 1 else 1

                    # send the appropriate packet including our pid
                    if new_local == 1:
                        client.send_ready()
                    else:
                        client.send_not_ready()

                    # also update the local cached lobby list immediately so UI reflects toggle
                    # rebuild client.lobby_players replacing our entry (or append if missing)
                    found = False
                    updated = []
                    for pid, r in client.lobby_players:
                        if pid == client.player_id:
                            updated.append((pid, new_local))
                            found = True
                        else:
                            updated.append((pid, r))
                    if not found:
                        updated.append((client.player_id, new_local))
                    client.lobby_players = updated


    WIN.fill((15, 15, 20))
    own_ready = next((r for (pid, r) in client.lobby_players if pid == client.player_id), 0)
    # Title
    draw_text(WIN, "Game Lobby", 20, 20, FONT)

    draw_text(WIN, "← / → to change color", 20, 320, SMALL)

    # Ready status
    draw_text(WIN, f"Your status: {'READY' if own_ready == 1 else 'Not Ready'}", 300, 20, SMALL, (0,255,0) if own_ready == 1 else (200,200,200))

    # Player list
    draw_text(WIN, "Players:", 20, 70, FONT)
    y = 110
    for pid, ready in client.lobby_players:
        status = "READY" if ready == 1 else "Not Ready"
        color = (0,255,0) if ready == 1 else (200,200,200)
        draw_text(WIN, f"Player {pid} - {status}", 40, y, SMALL, color)
        y += 30

    for pid, pdata in client.player_states.items():
        px = pdata.get("x")
        py = pdata.get("y")
        pcolor = pdata.get("color")

        if px is None or py is None or pcolor is None:
            continue

        img = DOT_IMAGES.get(pcolor)
        if img:
            rect = img.get_rect(center=(client.player_states[pid]["x"], client.player_states[pid]["y"]))
            WIN.blit(img, rect)


    # Countdown display
    if client.countdown_value is not None:
        draw_text(WIN, f"Game starting in: {client.countdown_value}",
                300, 60, FONT, (255, 255, 0))

    # Instructions
    draw_text(WIN, "Press SPACE to toggle ready", 20, 350, SMALL)

    # Most recent message
    if client.message_log:
        draw_text(WIN, f"Last message: {client.message_log[-1]}", 20, 300, SMALL)

    pygame.display.flip()

    if event.type == pygame.QUIT:
        if client.player_id is not None:
            msg = f"DISCONNECT,{client.player_id}".encode("utf-8")
            client.sock.sendto(msg, (SERVER_IP, SERVER_PORT))
        running = False

    if time.time() - client.last_heartbeat > 0.5:
        msg = f"HEARTBEAT,{client.player_id}".encode("utf-8")
        client.sock.sendto(msg, (SERVER_IP, SERVER_PORT))
        client.last_heartbeat = time.time()

client.close()
pygame.quit()