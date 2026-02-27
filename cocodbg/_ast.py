import ast
import sys
import traceback

_RESERVED_GLOBALS = {
    "dut", "clk", "cocotb", "Timer", "RisingEdge", "FallingEdge",
    "Clock", "BinaryValue",
    "start_clock", "stop_clock", "set_clock_rate", "step", "run_for", "cycles",
    "as_uint", "as_sint",
    "start_vcd", "stop_vcd", "gen_vcd", "view_vcd",
    "gtkwave_live_start", "gtkwave_live_stop",
    "wait_until", "show_sigs", "show_vars", "list_signals", "sim_time",
    "register_clock", "stop_all_clocks", "show_clocks",
}


def _collect_store_names(node: ast.AST) -> set[str]:
    names: set[str] = set()

    class StoreVisitor(ast.NodeVisitor):
        def visit_Name(self, n: ast.Name):
            if isinstance(n.ctx, ast.Store):
                names.add(n.id)
        def visit_Attribute(self, n: ast.Attribute):
            self.generic_visit(n.value)
        def visit_List(self, n: ast.List):
            for elt in n.elts:
                self.visit(elt)
        def visit_Tuple(self, n: ast.Tuple):
            for elt in n.elts:
                self.visit(elt)

    StoreVisitor().visit(node)
    return names


def _assigned_at_toplevel(tree: ast.Module) -> set[str]:
    """
    Return names assigned at the user's top level (not inside nested scopes).
    """
    assigned: set[str] = set()

    def visit_stmt(node: ast.stmt, nested_scope: bool):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not nested_scope:
                assigned.add(node.name)
            return
        if isinstance(node, ast.ClassDef):
            if not nested_scope:
                assigned.add(node.name)
            return
        if isinstance(node, ast.Lambda):
            return

        if not nested_scope and isinstance(node, ast.Import):
            for alias in node.names:
                assigned.add(alias.asname or alias.name.split(".", 1)[0])
        if not nested_scope and isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assigned.add(alias.asname or alias.name)

        if not nested_scope and isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            for t in targets:
                assigned.update(_collect_store_names(t))

        if not nested_scope and isinstance(node, (ast.For, ast.AsyncFor)):
            assigned.update(_collect_store_names(node.target))

        if not nested_scope and isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    assigned.update(_collect_store_names(item.optional_vars))

        if not nested_scope and isinstance(node, ast.Try):
            for h in node.handlers:
                if h.name:
                    assigned.add(h.name)

        if not nested_scope:
            class WalrusVisitor(ast.NodeVisitor):
                def visit_NamedExpr(self, n: ast.NamedExpr):
                    assigned.update(_collect_store_names(n.target))
                    self.generic_visit(n.value)
            WalrusVisitor().visit(node)

        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                for child in value:
                    if isinstance(child, ast.stmt):
                        visit_stmt(child, nested_scope)
            elif isinstance(value, ast.stmt):
                visit_stmt(value, nested_scope)

    for top in tree.body:
        visit_stmt(top, nested_scope=False)

    return {n for n in assigned if n not in _RESERVED_GLOBALS}


def wrap_code_as_async(code: str) -> str:
    """
    Wrap arbitrary user code into an async function __user_main__().
    - If the last statement is an expression, return it so the REPL shows its value.
    - Empty/comments/docstring-only bodies -> 'pass'.
    - Names assigned at the user's top level are declared 'global' so values persist.
    """
    def _assigned_at_toplevel_safe(src: str) -> set[str]:
        try:
            tree = ast.parse(src, mode="exec")
            return _assigned_at_toplevel(tree)
        except Exception:
            return set()

    def _is_only_comments_or_blank(src: str) -> bool:
        for ln in src.splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            return False
        return True

    def _is_only_docstrings(src: str) -> bool:
        try:
            tree = ast.parse(src, mode="exec")
        except Exception:
            return False
        if not tree.body:
            return False
        for node in tree.body:
            if not (isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant)
                    and isinstance(node.value.value, str)):
                return False
        return True

    globals_line = ""
    assigned = sorted(_assigned_at_toplevel_safe(code))
    if assigned:
        globals_line = "    global " + ", ".join(assigned) + "\n"

    if _is_only_comments_or_blank(code) or _is_only_docstrings(code):
        body = (globals_line + "    pass") if globals_line else "    pass"
        return f"async def __user_main__():\n{body}\n"

    try:
        tree = ast.parse(code, mode="exec")
        if tree.body and isinstance(tree.body[-1], ast.Expr) and hasattr(tree.body[-1], "lineno"):
            last = tree.body[-1]
            lines = code.splitlines()
            start = last.lineno - 1
            end = getattr(last, "end_lineno", last.lineno) - 1
            expr_src = ast.get_source_segment(code, last.value)
            if not expr_src:
                expr_src = "\n".join(lines[start:end + 1]).strip()
            kept = lines[:start] + lines[end + 1:]
            kept_src = "\n".join(kept)
            if _is_only_comments_or_blank(kept_src) or _is_only_docstrings(kept_src):
                indented = ["    pass"]
            else:
                indented = [("    " + ln) if ln.strip() else "    pass" for ln in kept]
            body = globals_line + ("\n".join(indented) + ("\n" if indented else "")) + f"    return ({expr_src})"
            return f"async def __user_main__():\n{body}\n"
    except Exception:
        pass

    indented = [("    " + ln) if ln.strip() else "    pass" for ln in code.splitlines()]
    body_core = "\n".join(indented) or "    pass"
    body = (globals_line + body_core) if globals_line else body_core
    return f"async def __user_main__():\n{body}\n"


def format_exception() -> str:
    return "".join(traceback.format_exception(*sys.exc_info()))
