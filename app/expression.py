"""SPDX-style license expression parser supporting AND / OR / WITH.

Grammar (operators are matched case-insensitively, license ids keep case)::

    expression := or_expr
    or_expr    := and_expr (OR and_expr)*
    and_expr   := with_expr (AND with_expr)*
    with_expr  := primary (WITH exception_id)?
    primary    := '(' or_expr ')' | license_id

AST nodes are tuples:
    ("license", license_id, exception_id_or_None)
    ("and", [node, ...])
    ("or",  [node, ...])

Parsing, AST traversal and OR-alternative expansion are all iterative
(explicit stacks on the heap): arbitrarily deeply nested expressions are
handled without growing the Python call stack, so no recursion-limit
raise or fixed depth cap is involved anywhere.
"""


class ExpressionError(ValueError):
    """Raised when an expression cannot be parsed."""

    def __init__(self, message, position):
        super().__init__(message)
        self.message = message
        self.position = position  # char offset, -1 means "end of input"


_KEYWORDS = {"AND", "OR", "WITH"}
_IDENT_CHARS = frozenset(".-+")


def _tokenize(text):
    tokens = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            tokens.append(("LPAREN", ch, i))
            i += 1
            continue
        if ch == ")":
            tokens.append(("RPAREN", ch, i))
            i += 1
            continue
        j = i
        while j < n and (text[j].isalnum() or text[j] in _IDENT_CHARS):
            j += 1
        if j == i:
            raise ExpressionError("unexpected character %r" % ch, i)
        word = text[i:j]
        upper = word.upper()
        if upper in _KEYWORDS:
            tokens.append((upper, word, i))
        else:
            tokens.append(("ID", word, i))
        i = j
    return tokens


def _collapse_and(items):
    return items[0] if len(items) == 1 else ("and", items)


def _collapse_or(branches):
    return branches[0] if len(branches) == 1 else ("or", branches)


def parse(text):
    """Parse *text* into an AST; raises ExpressionError on invalid input.

    Iterative equivalent of the recursive-descent grammar: one explicit
    frame per open parenthesis (plus the top level), each frame
    collecting the AND items of the current branch and the completed OR
    branches.  Nesting depth therefore consumes heap, not call stack,
    while the accepted language, the produced AST and every error
    message/position are exactly those of the recursive grammar.
    """
    if not isinstance(text, str) or not text.strip():
        raise ExpressionError("empty expression", 0)
    tokens = _tokenize(text)
    pos = 0

    EXPECT, HAVE, BOUND = 0, 1, 2  # BOUND: WITH exception already applied
    state = EXPECT
    node = None
    stack = [[[], []]]  # frames of [or branches, and items of current branch]
    while True:
        if pos < len(tokens):
            kind, value, at = tokens[pos]
        else:
            kind, value, at = None, None, -1

        if state == EXPECT:
            if kind == "ID":
                node = ("license", value, None)
                pos += 1
                state = HAVE
                continue
            if kind == "LPAREN":
                stack.append([[], []])
                pos += 1
                continue
            if kind is None:
                raise ExpressionError("unexpected end of expression", -1)
            raise ExpressionError("expected license id or '('", at)

        # HAVE / BOUND: a complete with_expr is at hand
        if kind == "WITH" and state == HAVE:
            pos += 1
            if pos < len(tokens):
                kind2, value2, at2 = tokens[pos]
            else:
                kind2, value2, at2 = None, None, -1
            if kind2 != "ID":
                raise ExpressionError("expected exception name after WITH", at2)
            if node[0] != "license":
                raise ExpressionError("WITH must follow a license id", at2)
            node = ("license", node[1], value2)
            pos += 1
            state = BOUND
            continue
        if kind == "AND":
            stack[-1][1].append(node)
            node = None
            pos += 1
            state = EXPECT
            continue
        if kind == "OR":
            branches, items = stack[-1]
            items.append(node)
            branches.append(_collapse_and(items))
            stack[-1] = [branches, []]
            node = None
            pos += 1
            state = EXPECT
            continue
        if kind == "RPAREN" and len(stack) > 1:
            branches, items = stack.pop()
            items.append(node)
            branches.append(_collapse_and(items))
            node = _collapse_or(branches)
            pos += 1
            state = HAVE
            continue
        if kind is None and len(stack) == 1:
            branches, items = stack[0]
            items.append(node)
            branches.append(_collapse_and(items))
            return _collapse_or(branches)
        # anything else is out of place after a complete expression
        if len(stack) > 1:
            raise ExpressionError("missing closing parenthesis", at)
        raise ExpressionError("unexpected trailing token", at)


def _walk(node):
    """Pre-order traversal of the AST (explicit stack, no call stack)."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if current[0] in ("and", "or"):
            stack.extend(reversed(current[1]))


def license_ids(ast):
    """Sorted unique license ids referenced by the expression."""
    return sorted({n[1] for n in _walk(ast) if n[0] == "license"})


def exception_ids(ast):
    """Sorted unique exception ids referenced via WITH."""
    return sorted({n[2] for n in _walk(ast) if n[0] == "license" and n[2]})


def alternatives(node):
    """Enumerate OR branches.

    Returns a list of alternatives; each alternative is a list of
    ``(license_id, exception_id_or_None)`` leaves that are implicitly
    AND-combined.  A pure AND expression yields exactly one alternative.

    Iterative post-order evaluation: subtree results accumulate on an
    explicit value stack, so deeply nested expressions never touch the
    Python call stack.  The produced list (contents and order) is
    identical to the recursive formulation.
    """
    values = []  # alternatives lists of completed subtrees
    work = [(node, False)]
    while work:
        current, expanded = work.pop()
        kind = current[0]
        if kind == "license":
            values.append([[(current[1], current[2])]])
            continue
        children = current[1]
        if not expanded:
            work.append((current, True))
            for child in reversed(children):
                work.append((child, False))
            continue
        base = len(values) - len(children)
        subs = values[base:]
        del values[base:]
        if kind == "or":
            out = []
            for sub in subs:
                out.extend(sub)
            values.append(out)
        else:  # "and": cartesian product of child alternatives
            combos = [[]]
            for sub in subs:
                combos = [head + tail for head in combos for tail in sub]
            values.append(combos)
    return values[0]
