import asyncio

import pytest

from cocodbg._ast import wrap_code_as_async


def run(code: str, env: dict | None = None):
    """Compile the wrapper, run it and return the value the REPL would show."""
    env = {} if env is None else env
    exec(compile(wrap_code_as_async(code), "<wrapped>", "exec"), env)
    return asyncio.run(env["__user_main__"]()), env


@pytest.mark.parametrize("code", [
    "x = 1\nif x:\n\n    y = 2\nx",
    "def f():\n    a = 1\n\n    return a\nf()",
    "for i in range(2):\n    pass\n\n\nprint(i)",
    "# only a comment",
    "",
    "'''docstring only'''",
])
def test_wrapper_compiles(code):
    compile(wrap_code_as_async(code), "<wrapped>", "exec")


def test_blank_line_inside_block_is_kept():
    value, _ = run("x = 1\nif x:\n\n    y = 2\ny")
    assert value == 2


def test_multiline_string_is_not_indented():
    value, _ = run("s = '''a\n\nb'''\ns")
    assert value == "a\n\nb"


def test_multiline_fstring_is_not_indented():
    value, _ = run("n = 1\ns = f'''a\n{n}\nb'''\ns")
    assert value == "a\n1\nb"


def test_docstring_inside_function_is_not_indented():
    value, _ = run("def g():\n    '''d\n  e'''\n    return g.__doc__\ng()")
    assert value == "d\n  e"


def test_last_expression_is_returned_and_assignments_persist():
    value, env = run("a = 3\na + 1")
    assert value == 4
    assert env["a"] == 3


def test_syntax_error_surfaces_at_compile_time():
    with pytest.raises(SyntaxError):
        compile(wrap_code_as_async("if True"), "<wrapped>", "exec")
