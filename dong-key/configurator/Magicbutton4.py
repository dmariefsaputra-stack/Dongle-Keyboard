import tkinter as tk
from tkinter import messagebox
from tkinter import ttk
import serial
import serial.tools.list_ports
import threading
import queue
import time

# ========================================================================= #
# BRANDING & SUPPORT CONFIGURATION                                          #
# ========================================================================= #
DEVELOPER_NAME = "Dani Madya A.S."
DEVELOPER_CONTACT = "dm.ariefsaputra@gmail.com"

BG_DARK = "#121824"
BG_PANEL = "#1E293B"
TEXT_LIGHT = "#F8FAFC"
TEXT_MUTED = "#94A3B8"
ACCENT_BLUE = "#38BDF8"
ACCENT_GREEN = "#10B981"
ACCENT_RED = "#F87171"

TOTAL_MAX_CHARS = 80  # Expanded capacity limit

# Only characters the CH552 HID keyboard can actually type (US layout ASCII)
TYPEABLE_MIN = 0x20
TYPEABLE_MAX = 0x7E

# Explanations for the firmware's error replies
FIRMWARE_ERRORS = {
    "ERROR_TIMEOUT": "The transfer was interrupted before all data arrived.\n"
                     "Nothing was written; the passwords on the device are unchanged.",
    "ERROR_LIMIT":   f"The device rejected the data (more than {TOTAL_MAX_CHARS} characters in total).\n"
                     "Nothing was written.",
    "ERROR_CHAR":    "The device rejected a character it cannot type.\n"
                     "Nothing was written.",
    "ERROR_WRITE":   "Writing to the device memory failed.\n"
                     "The device will refuse to type until a successful update. Please try again.",
}

app_root = None
entry_pass1 = None
entry_pass2 = None
entry_pass3 = None
entry_pass4 = None
dropdown_port = None
label_counter = None
label_status = None
btn_update = None
btn_read = None

# Prevents a read and a write from using the serial port at the same time
port_lock = threading.Lock()
# ========================================================================= #


# ------------------------------------------------------------------------- #
# Thread-safe UI helpers (tkinter must only be touched from the main thread) #
# ------------------------------------------------------------------------- #
# Background threads never touch tkinter directly (not even .after()):
# they put work in this queue, and the main thread drains it every 50 ms.
ui_queue = queue.Queue()

def ui_call(fn):
    ui_queue.put(fn)

def process_ui_queue():
    try:
        while True:
            fn = ui_queue.get_nowait()
            try:
                fn()
            except Exception as e:
                print(f"UI update error: {e}")
    except queue.Empty:
        pass
    app_root.after(50, process_ui_queue)

def short_error(e):
    text = f"{type(e).__name__}: {e}"
    return text if len(text) <= 60 else text[:57] + "..."

def show_info(title, msg):
    ui_call(lambda: messagebox.showinfo(title, msg))

def show_warning(title, msg):
    ui_call(lambda: messagebox.showwarning(title, msg))

def show_error(title, msg):
    ui_call(lambda: messagebox.showerror(title, msg))

def set_status(text, color=TEXT_MUTED):
    ui_call(lambda: label_status.config(text=text, fg=color))

def set_buttons_enabled(enabled):
    state = "normal" if enabled else "disabled"
    ui_call(lambda: (btn_update.config(state=state), btn_read.config(state=state)))


def is_typeable(text):
    return all(TYPEABLE_MIN <= ord(c) <= TYPEABLE_MAX for c in text)

def all_entries():
    return (entry_pass1, entry_pass2, entry_pass3, entry_pass4)


def proceed_to_main():
    window_welcome.destroy()
    open_main_application()

def scan_com_ports():
    ports = list(serial.tools.list_ports.comports())
    port_list = [p.device for p in ports]
    dropdown_port['values'] = port_list
    if port_list:
        dropdown_port.current(0)
        on_port_selected()
    else:
        dropdown_port.set("No Ports Found")
        set_status("No serial ports found.", ACCENT_RED)

def on_port_selected(event=None):
    selected_port = dropdown_port.get()
    if selected_port and selected_port != "No Ports Found":
        threading.Thread(target=fetch_passwords_background, args=(selected_port,), daemon=True).start()

def manual_read_passwords():
    selected_port = dropdown_port.get()
    if selected_port == "No Ports Found" or not selected_port:
        messagebox.showerror("Error", "Please select a valid Serial COM Port first!")
        return
    threading.Thread(target=fetch_passwords_background, args=(selected_port, True), daemon=True).start()


# ------------------------------------------------------------------------- #
# READ                                                                       #
# ------------------------------------------------------------------------- #
def fetch_passwords_background(selected_port, manual_click=False):
    if not port_lock.acquire(blocking=False):
        if manual_click:
            show_warning("Busy", "Another operation is using the port. Please wait.")
        return
    set_buttons_enabled(False)
    set_status(f"Reading from {selected_port}...")
    try:
        # write_timeout: without it, a write to a device that isn't servicing
        # USB blocks forever and the app stays on "Reading..." indefinitely
        with serial.Serial(selected_port, 115200, timeout=2, write_timeout=2) as ser:
            time.sleep(1.5)
            ser.reset_input_buffer()
            ser.write(b'?')

            raw_lens = ser.read(4)
            if len(raw_lens) < 4:
                set_status("Device did not respond.", ACCENT_RED)
                if manual_click:
                    show_warning("Timeout", "Device did not respond to read query.")
                return

            lens = list(raw_lens)
            total = sum(lens)

            if 255 in lens or total > TOTAL_MAX_CHARS:
                set_status("Device memory has no valid layout.", ACCENT_RED)
                if manual_click:
                    show_warning("Blank EEPROM", "EEPROM contains no valid dynamic structural maps yet.")
                return

            if total == 0:
                # New firmware reports 0,0,0,0 when its stored data failed validation
                set_status("Stored data on device is invalid.", ACCENT_RED)
                show_warning("Invalid Data on Device",
                             "The device reports that its stored passwords are invalid or incomplete "
                             "(e.g. a previous update was interrupted).\n\n"
                             "Please enter all 4 passwords again and press 'Update Device'.")
                return

            data = ser.read(total)
            if len(data) < total:
                set_status("Incomplete data received.", ACCENT_RED)
                if manual_click:
                    show_warning("Timeout", "Device sent incomplete data. Please try again.")
                return

        # Split into the 4 slots and check every byte is typeable
        chunks, pos = [], 0
        for n in lens:
            chunks.append(data[pos:pos + n])
            pos += n

        passwords, bad_slots = [], []
        for i, chunk in enumerate(chunks):
            if all(TYPEABLE_MIN <= b <= TYPEABLE_MAX for b in chunk):
                passwords.append(chunk.decode('ascii'))
            else:
                passwords.append("")          # never show/re-save corrupted bytes
                bad_slots.append(i + 1)

        def update_ui_fields():
            for entry in all_entries():
                entry.config(validate="none")
            for entry, pw in zip(all_entries(), passwords):
                entry.delete(0, tk.END)
                entry.insert(0, pw)
            vcmd = (app_root.register(validate_input), '%P', '%W')
            for entry in all_entries():
                entry.config(validate="key", validatecommand=vcmd)
            update_char_counter_initial()

            if bad_slots:
                slots = ", ".join(str(s) for s in bad_slots)
                label_status.config(text=f"Corrupted slot(s): {slots}", fg=ACCENT_RED)
                messagebox.showwarning("Corrupted Data",
                                       f"Password slot(s) {slots} contain characters that cannot be typed "
                                       "(memory corruption).\n\nThose slots were cleared. Please re-enter "
                                       "them and press 'Update Device'.")
            else:
                label_status.config(text=f"Loaded from {selected_port}.", fg=ACCENT_GREEN)
                if manual_click:
                    messagebox.showinfo("Success", "Passwords successfully loaded from device memory!")

        ui_call(update_ui_fields)

    except serial.SerialTimeoutException:
        set_status("Device is not accepting data (firmware hung?).", ACCENT_RED)
        show_warning("Device Not Responding",
                     f"{selected_port} opened, but the device is not accepting data.\n\n"
                     "Unplug and replug the device, then press 'Read Layout'.\n"
                     "If it keeps happening, check the onboard LED: no blinking at all "
                     "means the firmware is stuck.")
    except Exception as e:
        set_status(short_error(e), ACCENT_RED)
        if manual_click:
            show_error("Read Failed", f"Could not sync with hardware.\nDetails: {e}")
    finally:
        port_lock.release()
        set_buttons_enabled(True)


# ------------------------------------------------------------------------- #
# INPUT VALIDATION                                                           #
# ------------------------------------------------------------------------- #
def validate_input(proposed_value, widget_name):
    if None in (entry_pass1, entry_pass2, entry_pass3, entry_pass4, label_counter):
        return True

    # Block characters the device cannot type (é, £, €, emoji, smart quotes, ...)
    if not is_typeable(proposed_value):
        label_counter.config(text="Only standard ASCII characters are allowed", fg=ACCENT_RED)
        return False

    p1 = proposed_value if widget_name == str(entry_pass1) else entry_pass1.get()
    p2 = proposed_value if widget_name == str(entry_pass2) else entry_pass2.get()
    p3 = proposed_value if widget_name == str(entry_pass3) else entry_pass3.get()
    p4 = proposed_value if widget_name == str(entry_pass4) else entry_pass4.get()

    total_len = len(p1) + len(p2) + len(p3) + len(p4)
    remaining = TOTAL_MAX_CHARS - total_len

    if remaining >= 0:
        label_counter.config(text=f"Remaining Characters: {remaining} / {TOTAL_MAX_CHARS}", fg=TEXT_MUTED)
        return True
    return False

def update_char_counter_initial():
    total_len = sum(len(e.get()) for e in all_entries())
    remaining = TOTAL_MAX_CHARS - total_len
    label_counter.config(text=f"Remaining Characters: {remaining} / {TOTAL_MAX_CHARS}", fg=TEXT_MUTED)

def toggle_password_visibility(entry_field, button_widget):
    if entry_field.cget('show') == '':
        entry_field.config(show='*'); button_widget.config(text="🕶️")
    else:
        entry_field.config(show=''); button_widget.config(text="👁️")


# ------------------------------------------------------------------------- #
# WRITE                                                                      #
# ------------------------------------------------------------------------- #
def send_serial_data_background(selected_port, payloads):
    if not port_lock.acquire(blocking=False):
        show_warning("Busy", "Another operation is using the port. Please wait.")
        return
    set_buttons_enabled(False)
    set_status(f"Writing to {selected_port}...")
    try:
        # timeout=10: the firmware buffers everything first, then writes and verifies
        with serial.Serial(selected_port, 115200, timeout=10, write_timeout=5) as ser:
            time.sleep(1.5)
            ser.reset_input_buffer()

            header_packet = bytes([ord('#')] + [len(p) for p in payloads])
            ser.write(header_packet)
            time.sleep(0.01)

            for p in payloads:
                for b in p:
                    ser.write(bytes([b]))
                    time.sleep(0.005)  # 5ms safe streaming delay
            ser.flush()

            response = ser.readline().decode('ascii', errors='replace').strip()

        if response == "OK":
            set_status("Saved to device.", ACCENT_GREEN)
            show_info("Success", "All 4 password macros successfully saved to CH552 EEPROM!")
        elif response in FIRMWARE_ERRORS:
            set_status(f"Device replied {response}.", ACCENT_RED)
            show_warning("Update Failed", f"{response}\n\n{FIRMWARE_ERRORS[response]}")
        elif response:
            set_status("Unexpected reply from device.", ACCENT_RED)
            show_warning("Update Failed", f"Unexpected reply from device: {response!r}")
        else:
            set_status("No reply from device.", ACCENT_RED)
            show_warning("No Response", "Data transmitted, but CH552 did not reply.\n"
                                        "Use 'Read Layout' to check what is stored.")
    except serial.SerialTimeoutException:
        set_status("Device is not accepting data.", ACCENT_RED)
        show_warning("Device Not Responding",
                     "The device stopped accepting data.\n"
                     "With the new firmware nothing is written until all data arrives, "
                     "so the stored passwords are unchanged.\n\n"
                     "Unplug and replug the device, then try again.")
    except Exception as e:
        set_status(short_error(e), ACCENT_RED)
        show_error("Transfer Failed", f"Internal error details:\n{e}")
    finally:
        port_lock.release()
        set_buttons_enabled(True)

def update_passwords():
    selected_port = dropdown_port.get()
    if selected_port == "No Ports Found" or not selected_port:
        messagebox.showerror("Error", "Please select a valid Serial Port!"); return

    passwords = [e.get() for e in all_entries()]

    if any(len(p) == 0 for p in passwords):
        messagebox.showerror("Validation Error", "Password slots cannot be left empty!"); return

    bad = [str(i + 1) for i, p in enumerate(passwords) if not is_typeable(p)]
    if bad:
        messagebox.showerror("Validation Error",
                             f"Slot(s) {', '.join(bad)} contain characters the device cannot type.\n"
                             "Use standard ASCII characters only (letters, digits, and symbols "
                             "on a US keyboard)."); return

    if sum(len(p) for p in passwords) > TOTAL_MAX_CHARS:
        messagebox.showerror("Validation Error", f"Total combined strings cannot exceed {TOTAL_MAX_CHARS} characters!"); return

    # Encode everything BEFORE opening the port, so a bad character can never
    # interrupt a transfer halfway through
    payloads = [p.encode('ascii') for p in passwords]

    threading.Thread(target=send_serial_data_background, args=(selected_port, payloads), daemon=True).start()


# ------------------------------------------------------------------------- #
# MAIN WINDOW                                                                #
# ------------------------------------------------------------------------- #
def open_main_application():
    global app_root, entry_pass1, entry_pass2, entry_pass3, entry_pass4
    global dropdown_port, label_counter, label_status, btn_update, btn_read

    root = tk.Tk()
    app_root = root
    root.title("CH552 DONG-KEY 4-Slot Configurator")
    root.geometry("360x535")
    root.resizable(False, False)
    root.configure(bg=BG_DARK)

    vcmd = (root.register(validate_input), '%P', '%W')

    tk.Label(root, text="CH552 4-Slot Configuration Engine", font=("Arial", 13, "bold"), bg=BG_DARK, fg=ACCENT_BLUE).pack(pady=(15, 5))

    # --- PORT SELECTION ---
    frame_p_layout = tk.Frame(root, bg=BG_DARK)
    frame_p_layout.pack(pady=5)
    tk.Label(frame_p_layout, text="Select Port:", font=("Arial", 10), bg=BG_DARK, fg=TEXT_LIGHT).grid(row=0, column=0, padx=5)
    dropdown_port = ttk.Combobox(frame_p_layout, width=12, state="readonly")
    dropdown_port.grid(row=0, column=1, padx=5)
    dropdown_port.bind("<<ComboboxSelected>>", on_port_selected)
    tk.Button(frame_p_layout, text="🔄 Scan", command=scan_com_ports, font=("Arial", 9, "bold"), bg=BG_PANEL, fg=TEXT_LIGHT, borderwidth=0, relief="flat", padx=8).grid(row=0, column=2, padx=5)

    # --- ROW INPUT GENERATOR FUNCTION ---
    def make_input_row(label_text, info_text):
        tk.Label(root, text=label_text, font=("Arial", 10, "bold"), bg=BG_DARK, fg=TEXT_LIGHT).pack(pady=(8, 0))
        frame = tk.Frame(root, bg=BG_DARK); frame.pack(pady=2)
        ent = tk.Entry(frame, font=("Arial", 11), width=26, justify="center", bg=BG_PANEL, fg=ACCENT_BLUE, insertbackground=TEXT_LIGHT, relief="flat", borderwidth=4, show="*")
        ent.grid(row=0, column=0)
        btn = tk.Button(frame, text="🕶️", font=("Arial", 10), bg=BG_PANEL, fg=TEXT_LIGHT, borderwidth=0, relief="flat", padx=5, pady=2)
        btn.grid(row=0, column=1, padx=(5, 0))
        btn.config(command=lambda: toggle_password_visibility(ent, btn))
        tk.Label(root, text=info_text, font=("Arial", 9, "italic"), bg=BG_DARK, fg=TEXT_MUTED).pack()
        return ent

    entry_pass1 = make_input_row("Password Macro 1:", "💡 Triggered by a Single-click")
    entry_pass2 = make_input_row("Password Macro 2:", "💡 Triggered by a Double-click")
    entry_pass3 = make_input_row("Password Macro 3:", "💡 Triggered by a Triple-click")
    entry_pass4 = make_input_row("Password Macro 4:", "💡 Triggered by holding the button down")

    label_counter = tk.Label(root, text="", font=("Arial", 10, "bold"), bg=BG_DARK, fg=TEXT_MUTED)
    label_counter.pack(pady=(15, 0))

    label_status = tk.Label(root, text="", font=("Arial", 9), bg=BG_DARK, fg=TEXT_MUTED)
    label_status.pack(pady=(4, 0))

    # --- BUTTONS ZONE ---
    frame_actions = tk.Frame(root, bg=BG_DARK); frame_actions.pack(pady=12)
    btn_read = tk.Button(frame_actions, text="📖 Read Layout", command=manual_read_passwords, bg=BG_PANEL, fg=ACCENT_BLUE, font=("Arial", 11, "bold"), borderwidth=0, relief="flat", padx=12, pady=8)
    btn_read.grid(row=0, column=0, padx=8)
    btn_update = tk.Button(frame_actions, text="Update Device", command=update_passwords, bg=ACCENT_GREEN, fg=TEXT_LIGHT, font=("Arial", 11, "bold"), borderwidth=0, relief="flat", padx=12, pady=8)
    btn_update.grid(row=0, column=1, padx=8)

    # Fields start EMPTY (no "Password" placeholder), so a failed auto-read can
    # never lead to accidentally overwriting the real passwords on the device.
    for entry in all_entries():
        entry.config(validate="key", validatecommand=vcmd)

    root.after(50, process_ui_queue)
    scan_com_ports()
    update_char_counter_initial()
    root.mainloop()

# --- INITIAL WELCOME SCREEN ---
window_welcome = tk.Tk()
window_welcome.title("Welcome")
window_welcome.geometry("380x220")
window_welcome.resizable(False, False)
window_welcome.configure(bg=BG_DARK)
tk.Label(window_welcome, text="CH552 4-Macro Configurator", font=("Arial", 14, "bold"), bg=BG_DARK, fg=ACCENT_BLUE).pack(pady=(15, 5))
frame_credits = tk.LabelFrame(window_welcome, text=" Support Credits ", font=("Arial", 9, "italic"), bg=BG_DARK, fg=TEXT_MUTED, padx=15, pady=10)
frame_credits.pack(fill="x", padx=25, pady=5)
tk.Label(frame_credits, text=f"Created by: {DEVELOPER_NAME}", font=("Arial", 10), bg=BG_DARK, fg=TEXT_LIGHT).pack(anchor="w")
tk.Label(frame_credits, text=f"Contact: {DEVELOPER_CONTACT}", font=("Arial", 9), bg=BG_DARK, fg=ACCENT_BLUE).pack(anchor="w", pady=(4, 0))
tk.Button(window_welcome, text="Open Settings Panel →", command=proceed_to_main, bg=BG_PANEL, fg=TEXT_LIGHT, font=("Arial", 10, "bold"), borderwidth=0, relief="flat", padx=12, pady=6).pack(pady=15)
window_welcome.mainloop()
