"""Tkinter control UI for the Sector Gap Analyzer pipeline."""

from __future__ import annotations

import queue
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from gui import config as config_module
from gui import pipeline
from gui.worker import DONE_SENTINEL, Worker

MAX_LOG_LINES = 5000
POLL_INTERVAL_MS = 100
SHUTDOWN_POLL_MS = 200
SHUTDOWN_TIMEOUT_MS = 20000


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"Sector Gap Analyzer {config_module.APP_VERSION}")
        self.root.geometry("900x700")

        self.worker = Worker()
        self.config: dict[str, Any] = config_module.load_config()

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.run_tab = ttk.Frame(self.notebook)
        self.settings_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.run_tab, text="Run")
        self.notebook.add(self.settings_tab, text="Settings")

        self._build_run_tab(self.run_tab)
        self._build_settings_tab(self.settings_tab)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(POLL_INTERVAL_MS, self._poll_log_queue)

    # ------------------------------------------------------------------
    # Run tab
    # ------------------------------------------------------------------
    def _build_run_tab(self, parent: ttk.Frame) -> None:
        # -- Mode --
        mode_frame = ttk.LabelFrame(parent, text="Mode")
        mode_frame.pack(fill="x", padx=6, pady=6)
        self.mode_frame = mode_frame
        self.mode_var = tk.StringVar(value=self.config.get("mode", "gap"))
        mode_row = ttk.Frame(mode_frame)
        mode_row.pack(fill="x", padx=6, pady=4)
        ttk.Radiobutton(
            mode_row, text="Gap", variable=self.mode_var, value="gap", command=self._refresh_mode
        ).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(
            mode_row, text="Spatial", variable=self.mode_var, value="spatial", command=self._refresh_mode
        ).pack(side="left")

        # -- Sector list (Gap mode) --
        sector_frame = ttk.LabelFrame(parent, text="Sectors")
        sector_frame.pack(fill="x", padx=6, pady=6)
        self.sector_frame = sector_frame

        list_row = ttk.Frame(sector_frame)
        list_row.pack(fill="x", padx=6, pady=6)

        self.sector_listbox = tk.Listbox(list_row, height=5, selectmode="extended")
        self.sector_listbox.pack(side="left", fill="both", expand=True)
        for sector in self.config.get("sectors", []):
            self.sector_listbox.insert("end", sector)

        list_buttons = ttk.Frame(list_row)
        list_buttons.pack(side="left", padx=6)
        ttk.Button(list_buttons, text="Remove selected", command=self._remove_selected_sectors).pack(
            fill="x", pady=2
        )
        ttk.Button(list_buttons, text="Clear all", command=self._clear_sectors).pack(fill="x", pady=2)

        entry_row = ttk.Frame(sector_frame)
        entry_row.pack(fill="x", padx=6, pady=(0, 6))
        self.sector_entry = ttk.Entry(entry_row)
        self.sector_entry.pack(side="left", fill="x", expand=True)
        self.sector_entry.bind("<Return>", lambda _event: self._add_sector())
        ttk.Button(entry_row, text="Add", command=self._add_sector).pack(side="left", padx=(6, 0))

        # -- Spatial search (Spatial mode) --
        spatial_frame = ttk.LabelFrame(parent, text="Spatial Search")
        self.spatial_frame = spatial_frame

        self.spatial_center_var = tk.StringVar(value=self.config.get("spatial_center_system", ""))
        self.spatial_radius_var = tk.StringVar(value=str(self.config.get("spatial_radius_ly", 20)))
        self.spatial_sector_override_var = tk.StringVar(value=self.config.get("spatial_sector_override", ""))

        spatial_grid = ttk.Frame(spatial_frame)
        spatial_grid.pack(fill="x", padx=6, pady=6)
        ttk.Label(spatial_grid, text="Center system:").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(spatial_grid, textvariable=self.spatial_center_var, width=40).grid(
            row=0, column=1, sticky="we", pady=2
        )
        ttk.Label(spatial_grid, text="Radius (ly):").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(spatial_grid, textvariable=self.spatial_radius_var, width=10).grid(
            row=1, column=1, sticky="w", pady=2
        )
        ttk.Label(spatial_grid, text="Sector override (optional):").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(spatial_grid, textvariable=self.spatial_sector_override_var, width=40).grid(
            row=2, column=1, sticky="we", pady=2
        )
        ttk.Label(
            spatial_frame,
            text="Sector is auto-detected from the center system name if left blank. "
                 "That sector must already be extracted (use Gap mode first).",
            wraplength=560, justify="left", foreground="#666666",
        ).pack(anchor="w", padx=6, pady=(0, 6))
        spatial_grid.columnconfigure(1, weight=1)

        # -- Stages --
        stages_frame = ttk.LabelFrame(parent, text="Stages")
        stages_frame.pack(fill="x", padx=6, pady=6)

        stages = self.config.get("stages", {})
        self.stage_vars: dict[str, tk.BooleanVar] = {
            "extract": tk.BooleanVar(value=stages.get("extract", True)),
            "bracketed_gaps": tk.BooleanVar(value=stages.get("bracketed_gaps", True)),
            "backward_extrap": tk.BooleanVar(value=stages.get("backward_extrap", True)),
            "forward_extrap": tk.BooleanVar(value=stages.get("forward_extrap", False)),
            "aggregate": tk.BooleanVar(value=stages.get("aggregate", True)),
        }
        labels = {
            "extract": "Extract sectors from galaxy dump",
            "bracketed_gaps": "Bracketed gaps (intra-sequence)",
            "backward_extrap": "Backward extrapolation",
            "forward_extrap": "Forward extrapolation (advanced)",
            "aggregate": "Aggregate master candidate list",
        }
        self.stage_checkbuttons: dict[str, ttk.Checkbutton] = {}
        for key in ("extract", "bracketed_gaps", "backward_extrap", "forward_extrap", "aggregate"):
            cb = ttk.Checkbutton(stages_frame, text=labels[key], variable=self.stage_vars[key])
            cb.pack(anchor="w", padx=6)
            self.stage_checkbuttons[key] = cb

        # -- Parameters --
        params_frame = ttk.LabelFrame(parent, text="Parameters")
        params_frame.pack(fill="x", padx=6, pady=6)

        self.max_bracket_width_var = tk.StringVar(value=str(self.config.get("max_bracket_width", 25)))
        self.extend_depth_var = tk.StringVar(value=str(self.config.get("extend_depth", 5)))
        self.max_forward_step_var = tk.StringVar(value=str(self.config.get("max_forward_step", 5)))
        self.dry_run_var = tk.BooleanVar(value=self.config.get("dry_run", True))

        grid = ttk.Frame(params_frame)
        grid.pack(fill="x", padx=6, pady=6)
        ttk.Label(grid, text="Max bracket width:").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.max_bracket_width_var, width=8).grid(row=0, column=1, sticky="w")
        ttk.Label(grid, text="Backward extend depth:").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.extend_depth_var, width=8).grid(row=1, column=1, sticky="w")
        ttk.Label(grid, text="Max forward step:").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.max_forward_step_var, width=8).grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(
            params_frame,
            text="Dry run (skip EDSM validation -- check candidate volume first)",
            variable=self.dry_run_var,
        ).pack(anchor="w", padx=6, pady=(0, 6))

        # -- Run / Cancel + log --
        controls = ttk.Frame(parent)
        controls.pack(fill="x", padx=6, pady=6)
        self.run_button = ttk.Button(controls, text="Run", command=self._on_run)
        self.run_button.pack(side="left")
        self.cancel_button = ttk.Button(controls, text="Cancel", command=self._on_cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(6, 0))
        self.status_label = ttk.Label(controls, text="Idle")
        self.status_label.pack(side="left", padx=12)

        log_frame = ttk.LabelFrame(parent, text="Log")
        log_frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.log_text = tk.Text(log_frame, height=18, state="disabled", wrap="word")
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        self._refresh_mode()

    def _refresh_mode(self) -> None:
        mode = self.mode_var.get()
        if mode == "spatial":
            self.sector_frame.pack_forget()
            if not self.spatial_frame.winfo_ismapped():
                self.spatial_frame.pack(fill="x", padx=6, pady=6, after=self.mode_frame)
            for key in ("extract", "forward_extrap"):
                self.stage_checkbuttons[key].configure(state="disabled")
        else:  # gap
            self.spatial_frame.pack_forget()
            if not self.sector_frame.winfo_ismapped():
                self.sector_frame.pack(fill="x", padx=6, pady=6, after=self.mode_frame)
            for key in ("extract", "forward_extrap"):
                self.stage_checkbuttons[key].configure(state="normal")

    def _add_sector(self) -> None:
        value = self.sector_entry.get().strip()
        if not value:
            return
        existing = self.sector_listbox.get(0, "end")
        if value not in existing:
            self.sector_listbox.insert("end", value)
        self.sector_entry.delete(0, "end")

    def _remove_selected_sectors(self) -> None:
        for index in reversed(self.sector_listbox.curselection()):
            self.sector_listbox.delete(index)

    def _clear_sectors(self) -> None:
        self.sector_listbox.delete(0, "end")

    # ------------------------------------------------------------------
    # Settings tab
    # ------------------------------------------------------------------
    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        self.project_dir_var = tk.StringVar(value=self.config.get("project_dir", ""))
        self.galaxy_dump_var = tk.StringVar(value=self.config.get("galaxy_dump_path", ""))

        frame = ttk.Frame(parent)
        frame.pack(fill="x", padx=12, pady=12)

        ttk.Label(frame, text="Project directory:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.project_dir_var, width=60).grid(row=0, column=1, sticky="we", pady=4)
        ttk.Button(frame, text="Browse...", command=self._browse_project_dir).grid(row=0, column=2, padx=(6, 0))

        ttk.Label(frame, text="Galaxy dump path:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.galaxy_dump_var, width=60).grid(row=1, column=1, sticky="we", pady=4)
        ttk.Button(frame, text="Browse...", command=self._browse_galaxy_dump).grid(row=1, column=2, padx=(6, 0))

        frame.columnconfigure(1, weight=1)

        ttk.Button(parent, text="Save settings", command=self._save_settings).pack(anchor="w", padx=12, pady=6)

    def _browse_project_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.project_dir_var.get() or ".")
        if chosen:
            self.project_dir_var.set(chosen)

    def _browse_galaxy_dump(self) -> None:
        chosen = filedialog.askopenfilename(
            initialdir=str(Path(self.galaxy_dump_var.get() or ".").parent),
            filetypes=[("Galaxy dump", "*.json.gz *.json"), ("All files", "*.*")],
        )
        if chosen:
            self.galaxy_dump_var.set(chosen)

    def _save_settings(self) -> None:
        self.config["project_dir"] = self.project_dir_var.get().strip()
        self.config["galaxy_dump_path"] = self.galaxy_dump_var.get().strip()
        config_module.save_config(self.config)
        messagebox.showinfo("Settings", "Settings saved.")

    # ------------------------------------------------------------------
    # Run / Cancel
    # ------------------------------------------------------------------
    def _collect_config(self) -> dict[str, Any]:
        def _int_or(value: str, default: int) -> int:
            try:
                return int(value)
            except ValueError:
                return default

        self.config["project_dir"] = self.project_dir_var.get().strip()
        self.config["galaxy_dump_path"] = self.galaxy_dump_var.get().strip()
        self.config["mode"] = self.mode_var.get()
        self.config["sectors"] = list(self.sector_listbox.get(0, "end"))
        self.config["spatial_center_system"] = self.spatial_center_var.get().strip()
        self.config["spatial_radius_ly"] = _int_or(self.spatial_radius_var.get(), 20)
        self.config["spatial_sector_override"] = self.spatial_sector_override_var.get().strip()
        self.config["max_bracket_width"] = _int_or(self.max_bracket_width_var.get(), 25)
        self.config["extend_depth"] = _int_or(self.extend_depth_var.get(), 5)
        self.config["max_forward_step"] = _int_or(self.max_forward_step_var.get(), 5)
        self.config["dry_run"] = bool(self.dry_run_var.get())
        self.config["stages"] = {key: var.get() for key, var in self.stage_vars.items()}
        return self.config

    def _on_run(self) -> None:
        if self.worker.is_running():
            return
        cfg = self._collect_config()
        mode = cfg.get("mode", "gap")

        project_dir = cfg["project_dir"].strip()
        if not project_dir:
            messagebox.showerror("Settings needed", "Set a project/workspace directory in the Settings tab first.")
            self.notebook.select(self.settings_tab)
            return
        Path(project_dir).mkdir(parents=True, exist_ok=True)

        if mode == "spatial":
            if not cfg["spatial_center_system"]:
                messagebox.showwarning("Center system required", "Enter a center system before running.")
                return
            if cfg["spatial_radius_ly"] <= 0:
                messagebox.showwarning("Invalid radius", "Radius (ly) must be greater than 0.")
                return
            run_label = f"spatial search around {cfg['spatial_center_system']!r} ({cfg['spatial_radius_ly']} ly)"
        else:
            if not cfg["sectors"]:
                messagebox.showwarning("No sectors", "Add at least one sector before running.")
                return

            galaxy_dump_path = cfg["galaxy_dump_path"].strip()
            if not galaxy_dump_path or not Path(galaxy_dump_path).exists():
                messagebox.showerror(
                    "Galaxy dump not found",
                    "No galaxy dump file found at:\n\n"
                    f"{galaxy_dump_path or '(not set)'}\n\n"
                    "Download the full Spansh galaxy dump (galaxy.json.gz) and save it there, "
                    "or use Browse in the Settings tab to point at your own copy.",
                )
                self.notebook.select(self.settings_tab)
                return
            run_label = ', '.join(cfg['sectors'])

        config_module.save_config(cfg)

        self._append_log(f"--- Starting run: {run_label} ---")
        started = self.worker.start(pipeline.run_pipeline, cfg)
        if not started:
            return
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_label.configure(text="Running...")

    def _on_cancel(self) -> None:
        if self.worker.is_running():
            self.worker.cancel()
            self.status_label.configure(text="Cancelling...")

    def _on_job_done(self, returncode: str) -> None:
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.status_label.configure(text=f"Idle (last run exit={returncode})")

    # ------------------------------------------------------------------
    # Log polling
    # ------------------------------------------------------------------
    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log_queue(self) -> None:
        try:
            while True:
                line = self.worker.log_queue.get_nowait()
                if line.startswith(DONE_SENTINEL + ":"):
                    self._on_job_done(line.split(":", 1)[1])
                else:
                    self._append_log(line)
        except queue.Empty:
            pass
        self.root.after(POLL_INTERVAL_MS, self._poll_log_queue)

    def _on_close(self) -> None:
        if not self.worker.is_running():
            self.root.destroy()
            return
        if not messagebox.askyesno(
            "Job running",
            "A job is still running. Cancel it and wait for it to stop before exiting?",
        ):
            return
        self.worker.cancel()
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="disabled")
        self.status_label.configure(text="Cancelling -- please wait before closing...")
        self._wait_for_shutdown(0)

    def _wait_for_shutdown(self, elapsed_ms: int) -> None:
        # Closing the window must not kill the worker thread mid-flight (it's
        # a daemon thread; destroying root ends the process outright) -- wait
        # for it to actually finish flushing/closing its db connections.
        if not self.worker.is_running():
            self.root.destroy()
            return
        if elapsed_ms >= SHUTDOWN_TIMEOUT_MS:
            if messagebox.askyesno(
                "Still running",
                "The job hasn't stopped yet. Force quit anyway? This may leave "
                "partially-written data in an inconsistent state.",
            ):
                self.root.destroy()
            else:
                self._wait_for_shutdown(0)
            return
        self.root.after(
            SHUTDOWN_POLL_MS, lambda: self._wait_for_shutdown(elapsed_ms + SHUTDOWN_POLL_MS)
        )


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
