"""左侧会话表：行 iid 就是会话 id，增量刷新 + 选中回调。"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import ttk


def _cell(value: object) -> str:
    """列值 → 显示文本：`None`（观测不到）与空串画成 `-`；**0 是有效值**（退出码 0、
    0 个成员），照画成 `0`，否则跟"观测不到"分不开。"""
    return "-" if value is None or value == "" else str(value)


class SessionTree(ttk.Frame):
    """会话表：列固定，每行的值由调用方给（谁的数据谁渲染）。"""

    COLUMNS: tuple[tuple[str, str, int], ...] = (
        ("command", "命令", 90),
        ("mode", "模式", 80),
        ("state", "状态", 70),
        ("exit", "退出码", 60),
        ("procs", "进程", 50),
    )

    def __init__(
        self, parent: tk.Misc, *, on_select: Callable[[str | None], None]
    ) -> None:
        super().__init__(parent)
        self._on_select = on_select
        self._tree = ttk.Treeview(
            self,
            columns=[column for column, _, _ in self.COLUMNS],
            show="headings",
            selectmode="browse",
        )
        for column, text, width in self.COLUMNS:
            self._tree.heading(column, text=text)
            self._tree.column(column, width=width, anchor=tk.W, stretch=True)
        self._tree.pack(fill=tk.BOTH, expand=True)
        self._tree.bind("<<TreeviewSelect>>", self._handle_select)

    def refresh(
        self,
        rows: Sequence[tuple[str, Sequence[object]]],
        *,
        selected: str | None = None,
    ) -> None:
        """增量刷新：只增删行、只在值变了时改写；`selected` 把程序侧的选中对齐回表里。

        值由调用方给原样数据，占位符（`None` / 空 → `-`）在这里画，口径只此一处。

        不能全表重建 + `selection_set`：`<<TreeviewSelect>>` 是**异步**派发的，删空表会让
        它带着空选中跑一次，把用户切走的会话弄丢（回调侧再比一次 id 兜住）。
        """
        alive = {iid for iid, _ in rows}
        for item in self._tree.get_children():
            if item not in alive:
                self._tree.delete(item)
        for index, (iid, values) in enumerate(rows):
            cells = tuple(_cell(value) for value in values)
            if not self._tree.exists(iid):
                self._tree.insert("", index, iid=iid, values=cells)
            elif self._tree.item(iid, "values") != cells:
                self._tree.item(iid, values=cells)
        if selected is not None and selected in alive and selected not in self._tree.selection():
            self._tree.selection_set(selected)

    def selected(self) -> str | None:
        selection = self._tree.selection()
        return selection[0] if selection else None

    def set_selection(self, iid: str | None) -> None:
        """程序侧改选中：只在这里动树，界面才不会与实际选中脱节。"""
        current = self._tree.selection()
        if iid is None:
            if current:
                self._tree.selection_remove(*current)
        elif iid not in current:
            self._tree.selection_set(iid)

    def values(self, iid: str) -> tuple[str, ...]:
        return tuple(self._tree.item(iid, "values"))

    def _handle_select(self, _event: object = None) -> None:
        self._on_select(self.selected())
