import ast
import io
import sys
import tokenize
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


def _string_continuation_lines(code: str) -> set[int]:
    """0-based indices of lines that continue a multi-line string token.

    Indenting these would change the string value, so the wrapper leaves them alone.
    """
    protected: set[int] = set()
    fstart = getattr(tokenize, "FSTRING_START", None)
    fend = getattr(tokenize, "FSTRING_END", None)
    depth = 0
    open_row = 0
    try:
        for tok in tokenize.generate_tokens(io.StringIO(code).readline):
            if tok.type == tokenize.STRING:
                protected.update(range(tok.start[0], tok.end[0]))
            elif fstart is not None and tok.type == fstart:
                if depth == 0:
                    open_row = tok.start[0]
                depth += 1
            elif fend is not None and tok.type == fend:
                depth -= 1
                if depth == 0:
                    protected.update(range(open_row, tok.end[0]))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return set()
    return protected


def _indent_lines(lines: list[str], protected: set[int]) -> list[str]:
    """Indent code lines by one level. Blank lines stay empty and string bodies stay intact."""
    out = []
    for i, ln in enumerate(lines):
        if i in protected or not ln.strip():
            out.append(ln if i in protected else "")
        else:
            out.append("    " + ln)
    return out


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
            keep_idx = [i for i in range(len(lines)) if i < start or i > end]
            kept = [lines[i] for i in keep_idx]
            protected_all = _string_continuation_lines(code)
            kept_protected = {n for n, i in enumerate(keep_idx) if i in protected_all}
            kept_src = "\n".join(kept)
            if _is_only_comments_or_blank(kept_src) or _is_only_docstrings(kept_src):
                indented = ["    pass"]
            else:
                indented = _indent_lines(kept, kept_protected)
            body = globals_line + ("\n".join(indented) + ("\n" if indented else "")) + f"    return ({expr_src})"
            return f"async def __user_main__():\n{body}\n"
    except Exception:
        pass

    indented = _indent_lines(code.splitlines(), _string_continuation_lines(code))
    body_core = "\n".join(indented) or "    pass"
    body = (globals_line + body_core) if globals_line else body_core
    return f"async def __user_main__():\n{body}\n"


def format_exception() -> str:
    return "".join(traceback.format_exception(*sys.exc_info()))
