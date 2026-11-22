"""Structural adapter for 39 more languages (PRD FR-040): functions, classes and inheritance, calls, imports and tests.

The deep adapters (Python, Java, Kotlin, C#, Go, TypeScript/JavaScript) read HTTP routes, outbound calls, schemas and data access. For the
languages below TIA reads the STRUCTURE only, with the tree-sitter grammars bundled in tree-sitter-language-pack 0.13 (offline: the 1.x
releases download grammars at run time, which an air-gapped or reproducible analysis cannot allow):

  each function or method   qualified name (module / class / impl prefix), signature (parameter names or arity, defaults), the names it
                            calls, a token hash that ignores comments and formatting, and its line range
  each class-like type      name and the types it extends or implements (INHERITS edges)
  each test                 a test function (by name, attribute or annotation, in a test file) or a test block of a test DSL
                            (RSpec / ExUnit / testthat / busted / Zig / D unittest ...), with the names it calls
  imports                   the modules a file imports (kept on its tests)

So a change to a function in any of these languages reaches its callers and the tests that call it, and a changed base type reaches its
subtypes, exactly as for the deep languages. What is NOT read here - HTTP routes, outbound HTTP calls, request/response schemas, data access -
is reported by the analysis as the unknown `structure_only_languages`, so a consumer written in one of these languages is never silently
missing from a report.

Grammars that hang or crash on unusual input are left out after a check (`tests/test_generic_adapter.py` parses awkward inputs with every
grammar in a separate process): COBOL loops forever in its scanner. Groovy's bundled grammar does not distinguish classes, methods and calls,
and Verilog/VHDL describe hardware rather than services, so they are not offered either.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
from typing import Optional

from ..models import TypeDecl

COMMENTS = ("comment", "line_comment", "block_comment", "multiline_comment")     # as tia.lang.common (not imported here: see _helpers)


def text_of(n) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def walk(n):
    stack = [n] if n is not None else []
    while stack:
        cur = stack.pop()
        yield cur
        stack.extend(reversed(cur.children))

VERSION = "gen-1"
MAX_BYTES = 2 * 1024 * 1024                 # larger files are reported, not parsed (generated code, minified bundles)
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*[!?]?")
LISP_IDENT = re.compile(r"[^\s()\[\]{}\"';`,]+")
PS_IDENT = re.compile(r"[A-Za-z_][\w-]*")
NAME_TYPES = {"identifier", "simple_identifier", "field_identifier", "property_identifier", "type_identifier", "constant", "name", "word",
              "bareword", "atom", "variable", "value_name", "simple_word", "lower_case_identifier", "IDENTIFIER", "sym_lit", "symbol",
              "function_name", "simple_name", "module_name"}
PARAM_FIELDS = ("parameters", "formal_parameters", "parameter_list", "lambda_list", "args", "patterns", "arguments")
CALLEE_FIELDS = ("function", "method", "name", "macro", "target", "expr", "entity", "subroutine", "command_name", "callee")
BASE_NODES = {"superclass", "base_clause", "class_interface_clause", "base_class_clause", "extends_clause", "inheritance_specifier",
              "interfaces", "superclasses", "implements_clause", "base_class", "class_inherits_decl", "class_heritage", "delegation_specifiers"}
BASE_LEAVES = {"type_identifier", "identifier", "constant", "name", "simple_identifier", "user_type", "qualified_identifier", "scoped_identifier",
               "simple_name"}
TEST_ATTR = re.compile(r"(^|[^A-Za-z])(test|isTest|Test|tokio::test|rstest|testMethod)([^A-Za-z]|$)")


def _last(text: str, rx=IDENT) -> Optional[str]:
    found = rx.findall(text or "")
    return found[-1] if found else None


def _hash_nodes(nodes) -> str:
    toks = [text_of(x) for n in nodes for x in walk(n) if x.child_count == 0 and x.type not in COMMENTS]
    return hashlib.sha1(" ".join(toks).encode()).hexdigest()[:16]


def _string_arg(n) -> Optional[str]:
    """The first string literal among n's descendants (a test block's title)."""
    for x in walk(n):
        if x.type in ("string", "string_literal", "interpreted_string_literal", "str_lit", "STRINGLITERALSINGLE", "line_string_literal",
                      "encapsed_string", "string_content", "quoted_content"):
            t = text_of(x).strip()
            return t.strip("\"'`") or None
    return None


class Lang:
    """Defaults that fit most grammars; a language overrides only what its grammar does differently."""
    grammar = ""
    label = ""
    ext: tuple = ()
    funcs: tuple = ()
    containers: tuple = ()
    types: tuple = ()
    calls: tuple = ()
    imports: tuple = ()
    prefix_statements: tuple = ()           # statements that set the qualname prefix for the siblings after them (Perl `package`)
    test_calls: tuple = ()                  # test-DSL calls whose block is one test
    named_args: bool = False                # callers pass arguments by name, so parameter names are part of the signature
    ident = IDENT

    # ---- names
    def name_of(self, n) -> Optional[str]:
        nm = n.child_by_field_name("name")
        if nm is not None:
            return self.clean(text_of(nm))
        d = n.child_by_field_name("declarator")
        while d is not None:                                  # C / C++ / Objective-C: int *(*f)(...) -> f
            inner = d.child_by_field_name("declarator")
            if inner is None or d.type in ("qualified_identifier", "destructor_name", "operator_name"):
                return self.clean(text_of(d))
            d = inner
        for c in n.named_children:
            if c.type in NAME_TYPES:
                return self.clean(text_of(c))
        return None

    def clean(self, s: str) -> Optional[str]:
        s = re.sub(r"\s+", "", s or "")
        s = re.sub(r"::|->|:|/|\\", ".", s).strip(".")
        return s or None

    def fn_name(self, n) -> Optional[str]:
        return self.name_of(n) if n.type in self.funcs else None

    def fn_nodes(self, n) -> list:
        """The nodes that make up the function (its signature and its body); most grammars keep both in one node."""
        return [n]

    def head_nodes(self, n) -> list:
        """Parts of a definition that look like calls but are its own header (Elixir `def pay(x)`, Lisp `(define (pay x) ...)`)."""
        return []

    def container_name(self, n) -> Optional[str]:
        return self.name_of(n) if n.type in self.containers else None

    def type_decl(self, n) -> Optional[tuple]:
        """(name, bases, kind) of a class-like declaration."""
        if n.type not in self.types:
            return None
        name = self.name_of(n)
        if not name:
            return None
        bases = []
        for c in n.children:
            if c.type in BASE_NODES:
                bases += [text_of(x) for x in walk(c) if x.type in BASE_LEAVES and x.child_count == 0 or x.type in ("constant", "type_identifier") and x.child_count == 0]
        sup = n.child_by_field_name("superclass")
        if sup is not None:
            bases += [text_of(x) for x in walk(sup) if x.child_count == 0 and x.type in BASE_LEAVES]
        return name, _uniq([b.split(".")[-1].split("::")[-1] for b in bases if b and b != name]), "interface" if "interface" in n.type else "class"

    # ---- calls
    def callee(self, n):
        for f in CALLEE_FIELDS:
            c = n.child_by_field_name(f)
            if c is not None:
                return c
        return n.named_children[0] if n.named_children else None

    def call_name(self, n) -> Optional[str]:
        if n.type not in self.calls:
            return None
        c = self.callee(n)
        return _last(text_of(c), self.ident) if c is not None else None

    # ---- signatures
    def param_nodes(self, n) -> list:
        for f in PARAM_FIELDS:
            p = n.child_by_field_name(f)
            if p is not None:
                return [c for c in p.named_children if c.type not in COMMENTS]
        d = n.child_by_field_name("declarator")
        while d is not None:
            p = d.child_by_field_name("parameters")
            if p is not None:
                return [c for c in p.named_children if c.type not in COMMENTS]
            d = d.child_by_field_name("declarator")
        return []

    def has_default(self, p) -> bool:
        if any(p.child_by_field_name(f) is not None for f in ("default_value", "default", "value")) or "optional" in p.type:
            return True
        t = text_of(p)
        nxt = p.next_sibling
        return bool(re.search(r"(?<![=!<>])=(?!=)|\\\\", t)) or (nxt is not None and text_of(nxt) == "=")

    def signature(self, n) -> dict:
        params = []
        for i, p in enumerate(self.param_nodes(n)):
            if self.named_args:
                nm = p.child_by_field_name("name") or p.child_by_field_name("pattern")
                name = self.clean(text_of(nm)) if nm is not None else _last(text_of(p).split("=")[0].split(":")[0], self.ident)
            else:
                name = f"p{i}"
            if name:
                params.append({"name": name, "kind": "positional", "default": self.has_default(p)})
        return {"params": params, "returns": None}

    # ---- tests and imports
    def is_test_fn(self, n, name: str, test_file: bool) -> bool:
        if self._attr_test(n):
            return True
        if not test_file:
            return False
        base = name.split(".")[-1]
        return bool(re.match(r"(?i)test", base)) or base.endswith("_test")

    def _attr_test(self, n) -> bool:
        """A test attribute or annotation on the declaration: #[test], @Test, @isTest, testMethod."""
        prev = n.prev_named_sibling
        while prev is not None and prev.type in ("attribute_item", "attribute", "annotation", "decorator", "marker_annotation"):
            if TEST_ATTR.search(text_of(prev)):
                return True
            prev = prev.prev_named_sibling
        for c in n.children:
            if c.type in ("modifiers", "attribute_list", "annotation", "attributes") and TEST_ATTR.search(text_of(c)):
                return True
        return False

    def test_block(self, n) -> Optional[tuple]:
        """(title, body) when n is a test-DSL call such as `it "pays" do ... end`."""
        if not self.test_calls or n.type not in self.calls:
            return None
        if self.call_name(n) not in self.test_calls:
            return None
        title = _string_arg(n)
        return (title, n) if title else None

    def import_name(self, n) -> Optional[str]:
        if n.type not in self.imports:
            return None
        s = _string_arg(n) or text_of(n)
        s = re.sub(r"^\s*(import|use|using|require|include|open|from|with|uses|library|source)\b", "", s).strip(" ;\"'<>()")
        return self.clean(re.sub(r"\.(rb|php|sh|lua|h|hpp|sol|dart|zig|jl|R|r|tcl)$", "", s.split()[0])) if s else None


def _uniq(xs):
    return list(dict.fromkeys(x for x in xs if x))


# ---- the languages ---------------------------------------------------------------------------------------------------------------------
class Rust(Lang):
    grammar, label, ext = "rust", "Rust", (".rs",)
    funcs, containers, types = ("function_item",), ("impl_item", "trait_item", "mod_item"), ("struct_item", "enum_item", "trait_item", "impl_item")
    calls, imports = ("call_expression", "macro_invocation", "token_tree"), ("use_declaration",)

    def call_name(self, n):
        if n.type == "token_tree":                                    # inside a macro: `helper (1, 2)` is identifier + token_tree
            prev = n.prev_sibling
            if n.parent is not None and n.parent.type == "token_tree" and prev is not None and prev.type == "identifier"                     and text_of(n).startswith("("):
                return text_of(prev)
            return None
        return super().call_name(n)

    def container_name(self, n):
        if n.type == "impl_item":
            t = n.child_by_field_name("type")
            return self.clean(re.sub(r"<.*", "", text_of(t))) if t is not None else None
        return super().container_name(n)

    def type_decl(self, n):
        if n.type == "impl_item":
            t, tr = n.child_by_field_name("type"), n.child_by_field_name("trait")
            if t is None or tr is None:
                return None
            return re.sub(r"<.*", "", text_of(t)), [re.sub(r"<.*", "", text_of(tr)).split("::")[-1]], "class"
        return super().type_decl(n)


class Ruby(Lang):
    grammar, label, ext = "ruby", "Ruby", (".rb", ".rake")
    funcs, containers, types = ("method", "singleton_method"), ("class", "module"), ("class", "module")
    calls, imports, test_calls, named_args = ("call",), ("call",), ("it", "specify", "test", "example", "scenario"), True

    def import_name(self, n):
        if self.call_name(n) in ("require", "require_relative"):
            s = _string_arg(n)
            return self.clean(s) if s else None
        return None


class Php(Lang):
    grammar, label, ext = "php", "PHP", (".php",)
    funcs = ("method_declaration", "function_definition")
    containers = types = ("class_declaration", "interface_declaration", "trait_declaration", "enum_declaration")
    calls = ("function_call_expression", "member_call_expression", "scoped_call_expression", "nullsafe_member_call_expression")
    imports, prefix_statements, named_args = ("namespace_use_clause",), ("namespace_definition",), True

    def container_name(self, n):
        if n.type == "namespace_definition" and n.child_by_field_name("body") is not None:
            return self.name_of(n)
        return super().container_name(n)

    def import_name(self, n):
        return self.clean(text_of(n).split(" as ")[0]) if n.type in self.imports else None


class Hack(Php):
    grammar, label, ext = "hack", "Hack", (".hack", ".hck")
    funcs = ("method_declaration", "function_declaration")
    containers = types = ("class_declaration", "interface_declaration", "trait_declaration")
    calls, imports, prefix_statements = ("call_expression",), ("namespace_use_declaration",), ()


class C(Lang):
    grammar, label, ext = "c", "C", (".c",)
    funcs, types, calls, imports = ("function_definition",), (), ("call_expression",), ("preproc_include",)


class Cpp(Lang):
    grammar, label, ext = "cpp", "C++", (".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hxx", ".hh", ".h++", ".ipp")
    funcs, calls, imports = ("function_definition",), ("call_expression",), ("preproc_include",)
    containers = types = ("class_specifier", "struct_specifier")

    def container_name(self, n):
        if n.type == "namespace_definition":
            return self.name_of(n)
        if n.type in self.containers and n.child_by_field_name("body") is not None:
            return self.name_of(n)
        return None

    def type_decl(self, n):
        return super().type_decl(n) if n.child_by_field_name("body") is not None else None

    def fn_name(self, n):
        name = super().fn_name(n)
        if name in ("TEST", "TEST_F", "TEST_P", "TYPED_TEST"):             # GoogleTest: TEST(Suite, Name)
            ps = [text_of(x) for x in self.param_nodes(n)]
            return "TEST." + ".".join(p.strip() for p in ps)
        return name

    def is_test_fn(self, n, name, test_file):
        return name.startswith("TEST.") or super().is_test_fn(n, name, test_file)


class Header(Cpp):
    """`.h` is C, C++ or Objective-C; the C++ grammar reads C headers too, and Objective-C headers are routed by their content."""
    label, ext = "C/C++ header", (".h",)


class ObjC(Lang):
    grammar, label, ext = "objc", "Objective-C", (".m", ".mm")
    funcs, calls, imports = ("function_definition", "method_definition", "method_declaration"), ("call_expression", "message_expression"), ("preproc_include",)
    containers = types = ("class_implementation", "class_interface", "protocol_declaration")

    def name_of(self, n):
        if n.type in ("method_definition", "method_declaration"):
            parts = [text_of(c) for c in n.named_children if c.type == "identifier"]
            return parts[0] if parts else None              # the first selector keyword: what a message expression names
        if n.type in self.containers:
            ids = [c for c in n.named_children if c.type == "identifier"]
            return text_of(ids[0]) if ids else None
        return super().name_of(n)

    def clean(self, s):
        return re.sub(r"\s+", "", s or "") or None

    def call_name(self, n):
        if n.type == "message_expression":
            m = n.child_by_field_name("method")
            return text_of(m) if m is not None else None
        return super().call_name(n)

    def fn_nodes(self, n):
        return [n] if n.type != "method_declaration" else []         # a declaration in @interface has no body


class Scala(Lang):
    grammar, label, ext = "scala", "Scala", (".scala", ".sc")
    funcs = ("function_definition",)
    containers = types = ("class_definition", "object_definition", "trait_definition")
    calls, imports, test_calls, named_args = ("call_expression",), ("import_declaration",), ("test", "it", "describe", "should"), True

    def test_block(self, n):
        hit = super().test_block(n)
        if hit and n.parent is not None and n.parent.type == "call_expression":
            return hit[0], n.parent
        return hit


class Swift(Lang):
    grammar, label, ext = "swift", "Swift", (".swift",)
    funcs = ("function_declaration", "init_declaration")
    containers = types = ("class_declaration", "protocol_declaration")
    calls, imports, named_args = ("call_expression",), ("import_declaration",), True

    def param_nodes(self, n):
        return [c for c in n.named_children if c.type == "parameter"]

    def call_name(self, n):
        if n.type != "call_expression" or not n.named_children:
            return None
        f = n.named_children[0]
        if f.type == "navigation_expression":
            s = f.child_by_field_name("suffix")
            return _last(text_of(s)) if s is not None else None
        return _last(text_of(f))

    def fn_name(self, n):
        if n.type == "init_declaration":
            return "init"
        return super().fn_name(n)


class Dart(Lang):
    grammar, label, ext = "dart", "Dart", (".dart",)
    funcs, containers = ("function_signature",), ("class_definition", "mixin_declaration", "extension_declaration")
    types, calls, imports = ("class_definition", "mixin_declaration"), ("selector",), ("import_or_export",)
    test_calls, named_args = ("test", "testWidgets"), True

    def fn_nodes(self, n):
        holder = n.parent if n.parent is not None and n.parent.type == "method_signature" else n
        body = holder.next_named_sibling
        return [holder, body] if body is not None and body.type == "function_body" else [holder]

    def param_nodes(self, n):
        out = []
        for c in n.named_children:
            if c.type == "formal_parameter_list":
                for p in c.named_children:
                    out += [q for q in p.named_children if q.type == "formal_parameter"] if p.type.startswith("optional") else [p]
        return out

    def call_name(self, n):
        if n.type != "selector" or not any(c.type == "argument_part" for c in n.named_children):
            return None
        prev = n.prev_named_sibling
        if prev is None:
            return None
        return _last(text_of(prev)) if prev.type in ("identifier", "selector") else None

    def test_block(self, n):
        if self.call_name(n) in self.test_calls:
            title = _string_arg(n)
            return (title, n) if title else None
        return None


class Lua(Lang):
    grammar, label, ext = "lua", "Lua", (".lua",)
    funcs, calls, test_calls = ("function_declaration",), ("function_call",), ("it", "test")

    def import_name(self, n):
        if n.type == "function_call" and self.call_name(n) == "require":
            s = _string_arg(n)
            return self.clean(s) if s else None
        return None

    imports = ("function_call",)


class Elixir(Lang):
    grammar, label, ext = "elixir", "Elixir", (".ex", ".exs")
    calls, imports, test_calls = ("call",), ("call",), ("test",)
    DEFS = ("def", "defp", "defmacro", "defmacrop", "defguard")
    KEYWORDS = DEFS + ("defmodule", "alias", "import", "use", "require", "describe", "test", "setup", "defstruct", "defimpl", "defprotocol")

    def _target(self, n):
        t = n.child_by_field_name("target")
        return text_of(t) if t is not None and t.type == "identifier" else None

    def _head(self, n):
        args = next((c for c in n.named_children if c.type == "arguments"), None)
        return args.named_children[0] if args is not None and args.named_children else None

    def fn_name(self, n):
        if n.type != "call" or self._target(n) not in self.DEFS:
            return None
        h = self._head(n)
        if h is not None and h.type == "binary_operator":              # def f(x) when is_integer(x)
            h = h.child_by_field_name("left")
        if h is None:
            return None
        return text_of(h.child_by_field_name("target")) if h.type == "call" and h.child_by_field_name("target") is not None else _last(text_of(h))

    def container_name(self, n):
        if n.type == "call" and self._target(n) in ("defmodule", "defprotocol", "defimpl"):
            h = self._head(n)
            return text_of(h) if h is not None else None
        return None

    def head_nodes(self, n):
        return [h for h in (self._head(n),) if h is not None]

    def param_nodes(self, n):
        h = self._head(n)
        if h is not None and h.type == "binary_operator":
            h = h.child_by_field_name("left")
        args = next((c for c in h.named_children if c.type == "arguments"), None) if h is not None else None
        return list(args.named_children) if args is not None else []

    def call_name(self, n):
        if n.type != "call":
            return None
        t = n.child_by_field_name("target")
        if t is None:
            return None
        if t.type == "dot":
            r = t.child_by_field_name("right")
            return text_of(r) if r is not None else None
        name = text_of(t)
        return None if name in self.KEYWORDS else _last(name)

    def test_block(self, n):
        if n.type == "call" and self._target(n) == "test":
            title = _string_arg(self._head(n)) if self._head(n) is not None else None
            return (title, n) if title else None
        return None

    def import_name(self, n):
        if n.type == "call" and self._target(n) in ("alias", "import", "use", "require"):
            h = self._head(n)
            return text_of(h) if h is not None else None
        return None


class Erlang(Lang):
    grammar, label, ext = "erlang", "Erlang", (".erl", ".hrl")
    funcs, calls = ("fun_decl",), ("call",)

    def fn_name(self, n):
        if n.type != "fun_decl":
            return None
        cl = n.child_by_field_name("clause") or (n.named_children[0] if n.named_children else None)
        nm = cl.child_by_field_name("name") if cl is not None else None
        return text_of(nm) if nm is not None else None

    def param_nodes(self, n):
        cl = n.child_by_field_name("clause")
        a = cl.child_by_field_name("args") if cl is not None else None
        return list(a.named_children) if a is not None else []

    def call_name(self, n):
        if n.type != "call":
            return None
        e = n.child_by_field_name("expr")
        if e is not None and e.type == "remote":
            f = e.child_by_field_name("fun")
            return text_of(f) if f is not None else None
        return text_of(e) if e is not None and e.type == "atom" else None


class Haskell(Lang):
    grammar, label, ext = "haskell", "Haskell", (".hs", ".lhs")
    funcs, calls, imports = ("function", "bind"), ("apply",), ("import",)

    def fn_name(self, n):
        if n.type not in self.funcs or n.child_by_field_name("name") is None:
            return None                                               # `Int -> Int` in a type signature is also a "function" node
        return text_of(n.child_by_field_name("name"))

    def call_name(self, n):
        if n.type != "apply":
            return None
        f = n.child_by_field_name("function")
        while f is not None and f.type == "apply":                     # f a b is apply(apply(f, a), b)
            f = f.child_by_field_name("function")
        return _last(text_of(f)) if f is not None else None

    def param_nodes(self, n):
        p = n.child_by_field_name("patterns")
        return list(p.named_children) if p is not None else []


class OCaml(Lang):
    grammar, label, ext = "ocaml", "OCaml", (".ml", ".mli")
    funcs, containers, calls, imports = ("let_binding",), ("module_binding",), ("application_expression",), ("open_module",)

    def fn_name(self, n):
        if n.type != "let_binding":
            return None
        has_params = any(c.type == "parameter" for c in n.named_children)
        body = n.child_by_field_name("body")
        if not has_params and (body is None or body.type not in ("fun_expression", "function_expression")):
            return None                                               # a value, not a function
        p = n.child_by_field_name("pattern")
        return text_of(p) if p is not None else None

    def container_name(self, n):
        if n.type == "module_binding":
            m = next((c for c in n.named_children if c.type == "module_name"), None)
            return text_of(m) if m is not None else None
        return None

    def param_nodes(self, n):
        return [c for c in n.named_children if c.type == "parameter"]


class FSharp(Lang):
    grammar, label, ext = "fsharp", "F#", (".fs", ".fsi", ".fsx")
    calls, imports = ("application_expression",), ("import_decl",)
    containers = ("named_module", "module_defn", "anon_type_defn")
    types = ("anon_type_defn",)

    def fn_name(self, n):
        if n.type == "function_or_value_defn":
            left = next((c for c in n.named_children if c.type == "function_declaration_left"), None)
            if left is None:
                return None
            ident = next((c for c in left.named_children if c.type == "identifier"), None)
            return text_of(ident) if ident is not None else None
        if n.type == "method_or_prop_defn":
            p = n.child_by_field_name("name")
            m = p.child_by_field_name("method") if p is not None else None
            return text_of(m) if m is not None else (text_of(p) if p is not None else None)
        return None

    def name_of(self, n):
        if n.type == "anon_type_defn":
            t = next((c for c in n.named_children if c.type == "type_name"), None)
            return _last(text_of(t)) if t is not None else None
        return super().name_of(n)

    def param_nodes(self, n):
        left = next((c for c in n.named_children if c.type == "function_declaration_left"), None)
        pats = next((c for c in left.named_children if c.type == "argument_patterns"), None) if left is not None else None
        return list(pats.named_children) if pats is not None else []

    def call_name(self, n):
        if n.type != "application_expression" or not n.named_children:
            return None
        return _last(text_of(n.named_children[0]))


class Elm(Lang):
    grammar, label, ext = "elm", "Elm", (".elm",)
    funcs, calls, imports = ("value_declaration",), ("function_call_expr",), ("import_clause",)

    def fn_name(self, n):
        if n.type != "value_declaration":
            return None
        left = next((c for c in n.named_children if c.type == "function_declaration_left"), None)
        ident = next((c for c in left.named_children if c.type == "lower_case_identifier"), None) if left is not None else None
        return text_of(ident) if ident is not None else None

    def param_nodes(self, n):
        left = next((c for c in n.named_children if c.type == "function_declaration_left"), None)
        return [c for c in left.named_children if c.type != "lower_case_identifier"] if left is not None else []

    def call_name(self, n):
        if n.type != "function_call_expr":
            return None
        t = n.child_by_field_name("target")
        return _last(text_of(t)) if t is not None else None


class R(Lang):
    grammar, label, ext = "r", "R", (".r", ".R")
    calls, imports, test_calls, named_args = ("call",), ("call",), ("test_that", "it"), True

    def fn_name(self, n):
        if n.type != "binary_operator":
            return None
        rhs, lhs = n.child_by_field_name("rhs"), n.child_by_field_name("lhs")
        if rhs is None or lhs is None or rhs.type != "function_definition":
            return None
        return self.clean(text_of(lhs).strip("`"))

    def param_nodes(self, n):
        fd = n.child_by_field_name("rhs")
        p = fd.child_by_field_name("parameters") if fd is not None else None
        return [c for c in p.named_children if c.type == "parameter"] if p is not None else []

    def import_name(self, n):
        if n.type == "call" and self.call_name(n) in ("library", "require", "source"):
            a = n.child_by_field_name("arguments")
            return _last(text_of(a)) if a is not None else None
        return None


class Julia(Lang):
    grammar, label, ext = "julia", "Julia", (".jl",)
    containers, types = ("module_definition",), ("struct_definition", "abstract_definition")
    calls, imports, named_args = ("call_expression",), ("using_statement", "import_statement"), True

    def fn_name(self, n):
        if n.type == "function_definition":
            sig = next((c for c in n.named_children if c.type == "signature"), None)
            call = next((c for c in walk(sig) if c.type == "call_expression"), None) if sig is not None else None
            return self.clean(text_of(call.named_children[0])) if call is not None and call.named_children else None
        if n.type == "assignment" and n.named_children and n.named_children[0].type == "call_expression" \
                and n.parent is not None and n.parent.type in ("source_file", "block", "module_definition"):
            call = n.named_children[0]                                  # pay(amount) = helper(amount)
            return self.clean(text_of(call.named_children[0])) if call.named_children else None
        return None

    def _call(self, n):
        if n.type == "function_definition":
            sig = next((c for c in n.named_children if c.type == "signature"), None)
            return next((c for c in walk(sig) if c.type == "call_expression"), None) if sig is not None else None
        return n.named_children[0] if n.named_children else None

    def param_nodes(self, n):
        call = self._call(n)
        args = next((c for c in call.named_children if c.type == "argument_list"), None) if call is not None else None
        return list(args.named_children) if args is not None else []

    def fn_nodes(self, n):
        return [n]

    def head_nodes(self, n):
        call = self._call(n)
        return [call] if call is not None else []

    def type_decl(self, n):
        if n.type not in self.types:
            return None
        head = next((c for c in n.named_children if c.type == "type_head"), None)
        if head is None:
            return None
        ids = [text_of(x) for x in walk(head) if x.type == "identifier"]
        return (ids[0], ids[1:2], "class") if ids else None

    def container_name(self, n):
        if n.type == "module_definition":
            ident = next((c for c in n.named_children if c.type == "identifier"), None)
            return text_of(ident) if ident is not None else None
        return None

    def test_block(self, n):
        if n.type == "macrocall_expression" and "testset" in text_of(n.named_children[0] if n.named_children else None):
            title = _string_arg(n)
            return (title, n) if title else None
        return None


class Perl(Lang):
    grammar, label, ext = "perl", "Perl", (".pl", ".pm", ".t")
    funcs, calls, imports = ("subroutine_declaration_statement",), ("function_call_expression", "method_call_expression"), ("use_statement",)
    prefix_statements = ("package_statement",)

    def call_name(self, n):
        if n.type == "method_call_expression":
            m = n.child_by_field_name("method")
            return text_of(m) if m is not None else None
        return super().call_name(n)

    def signature(self, n):
        return {"params": [], "returns": None}                         # Perl subs take @_: no declared parameters


class Bash(Lang):
    grammar, label, ext = "bash", "Bash", (".sh", ".bash")
    funcs, calls, imports = ("function_definition",), ("command",), ("command",)

    def call_name(self, n):
        if n.type != "command":
            return None
        c = n.child_by_field_name("name")
        return _last(text_of(c), PS_IDENT) if c is not None else None

    def import_name(self, n):
        if self.call_name(n) in ("source", "."):
            a = n.child_by_field_name("argument")
            return self.clean(re.sub(r"\.(sh|bash)$", "", text_of(a).strip("\"'"))) if a is not None else None
        return None


class PowerShell(Lang):
    grammar, label, ext = "powershell", "PowerShell", (".ps1", ".psm1")
    funcs, calls, containers, types, named_args, ident = ("function_statement", "class_method_definition"), ("command",), ("class_statement",), ("class_statement",), True, PS_IDENT

    def name_of(self, n):
        if n.type == "function_statement":
            f = next((c for c in n.named_children if c.type == "function_name"), None)
            return text_of(f) if f is not None else None
        sn = next((c for c in n.named_children if c.type == "simple_name"), None)
        return text_of(sn).strip() if sn is not None else None

    def clean(self, s):
        return (s or "").strip() or None

    def type_decl(self, n):
        if n.type != "class_statement":
            return None
        names = [text_of(c).strip() for c in n.named_children if c.type == "simple_name"]
        return (names[0], names[1:2], "class") if names else None

    def call_name(self, n):
        if n.type != "command":
            return None
        c = n.child_by_field_name("command_name")
        return text_of(c).strip() if c is not None else None

    def param_nodes(self, n):
        return [x for x in walk(n) if x.type in ("script_parameter", "class_method_parameter")]


class Clojure(Lang):
    grammar, label, ext, ident = "clojure", "Clojure", (".clj", ".cljs", ".cljc", ".edn"), LISP_IDENT
    calls = ("list_lit",)
    DEFS = ("defn", "defn-", "defmacro", "defmulti", "defmethod")
    SPECIAL = DEFS + ("ns", "def", "let", "if", "do", "fn", "loop", "recur", "when", "cond", "deftest", "is", "testing", "quote")

    def _syms(self, n):
        return [c for c in n.named_children if c.type == "sym_lit"]

    def _head(self, n):
        return text_of(n.named_children[0].child_by_field_name("name")) if n.type == "list_lit" and n.named_children \
            and n.named_children[0].type == "sym_lit" and n.named_children[0].child_by_field_name("name") is not None else None

    def fn_name(self, n):
        if self._head(n) in self.DEFS and len(n.named_children) > 1:
            return text_of(n.named_children[1])
        return None

    def head_nodes(self, n):
        return [c for c in n.named_children if c.type == "vec_lit"][:1]

    def param_nodes(self, n):
        v = next((c for c in n.named_children if c.type == "vec_lit"), None)
        return [c for c in v.named_children if text_of(c) != "&"] if v is not None else []

    def call_name(self, n):
        h = self._head(n)
        if not h or h in self.SPECIAL:
            return None
        return h

    def test_block(self, n):
        if self._head(n) == "deftest" and len(n.named_children) > 1:
            return text_of(n.named_children[1]), n
        return None

    def import_name(self, n):
        return None


class CommonLisp(Lang):
    grammar, label, ext, ident = "commonlisp", "Common Lisp", (".lisp", ".lsp", ".asd"), LISP_IDENT
    funcs, calls = ("defun",), ("list_lit",)
    SPECIAL = ("defun", "defclass", "defpackage", "defvar", "defparameter", "let", "let*", "if", "progn", "lambda", "in-package", "quote")

    def fn_name(self, n):
        if n.type != "defun":
            return None
        h = next((c for c in n.named_children if c.type == "defun_header"), None)
        f = h.child_by_field_name("function_name") if h is not None else None
        return text_of(f) if f is not None else None

    def head_nodes(self, n):
        return [c for c in n.named_children if c.type == "defun_header"]

    def param_nodes(self, n):
        h = next((c for c in n.named_children if c.type == "defun_header"), None)
        ll = h.child_by_field_name("lambda_list") if h is not None else None
        return [c for c in ll.named_children if not text_of(c).startswith("&")] if ll is not None else []

    def call_name(self, n):
        if n.type != "list_lit" or not n.named_children or n.named_children[0].type != "sym_lit":
            return None
        h = text_of(n.named_children[0])
        return None if h.lower() in self.SPECIAL else h

    def type_decl(self, n):
        if n.type == "list_lit" and n.named_children and text_of(n.named_children[0]).lower() == "defclass" and len(n.named_children) > 2:
            bases = n.named_children[2]
            return text_of(n.named_children[1]), [text_of(c) for c in bases.named_children if c.type == "sym_lit"], "class"
        return None

    def clean(self, s):
        return (s or "").strip() or None


class Scheme(Lang):
    grammar, label, ext, ident = "scheme", "Scheme", (".scm", ".ss", ".sld"), LISP_IDENT
    calls = ("list",)
    SPECIAL = ("define", "lambda", "let", "let*", "letrec", "if", "cond", "begin", "import", "require", "quote", "set!", "and", "or")

    def fn_name(self, n):
        ch = n.named_children
        if n.type == "list" and len(ch) > 1 and text_of(ch[0]) in ("define", "define*") and ch[1].type == "list" and ch[1].named_children:
            return text_of(ch[1].named_children[0])
        return None

    def head_nodes(self, n):
        return [n.named_children[1]]

    def param_nodes(self, n):
        sig = n.named_children[1]
        return list(sig.named_children[1:])

    def call_name(self, n):
        if n.type != "list" or not n.named_children or n.named_children[0].type != "symbol":
            return None
        h = text_of(n.named_children[0])
        return None if h in self.SPECIAL else h

    def clean(self, s):
        return (s or "").strip() or None


class Racket(Scheme):
    grammar, label, ext = "racket", "Racket", (".rkt",)


class Zig(Lang):
    grammar, label, ext = "zig", "Zig", (".zig",)
    funcs, calls = ("FnProto",), ("SuffixExpr", "FieldOrFnCall")

    def fn_name(self, n):
        if n.type != "FnProto":
            return None
        f = n.child_by_field_name("function")
        return text_of(f) if f is not None else None

    def fn_nodes(self, n):
        body = n.next_named_sibling
        return [n, body] if body is not None and body.type == "Block" else [n]

    def param_nodes(self, n):
        pl = next((c for c in n.named_children if c.type == "ParamDeclList"), None)
        return [c for c in pl.named_children if c.type == "ParamDecl"] if pl is not None else []

    def call_name(self, n):
        if n.type == "FieldOrFnCall":
            f = n.child_by_field_name("function_call")
            return text_of(f) if f is not None else None
        if n.type == "SuffixExpr" and any(c.type == "FnCallArguments" for c in n.named_children):
            f = n.child_by_field_name("variable_type_function")
            return text_of(f) if f is not None else None
        return None

    def test_block(self, n):
        if n.type == "TestDecl":
            return (_string_arg(n) or f"test@{n.start_point[0] + 1}"), n
        return None



class Nim(Lang):
    grammar, label, ext = "nim", "Nim", (".nim", ".nims")
    funcs = ("proc_declaration", "func_declaration", "method_declaration", "iterator_declaration", "converter_declaration")
    calls, imports, test_calls, named_args = ("call",), ("import_statement",), ("test",), True

    def param_nodes(self, n):
        p = n.child_by_field_name("parameters")
        out = []
        for pd in (p.named_children if p is not None else []):
            syms = [x for x in walk(pd) if x.type == "symbol_declaration"]
            out += syms if len(syms) > 1 else [pd]
        return out

    def type_decl(self, n):
        if n.type != "type_declaration":
            return None
        sym = next((c for c in n.named_children if c.type == "type_symbol_declaration"), None)
        obj = next((c for c in n.named_children if c.type == "object_declaration"), None)
        if sym is None:
            return None
        inh = obj.child_by_field_name("inherits") if obj is not None else None
        return _last(text_of(sym)), [_last(text_of(inh))] if inh is not None else [], "class"


class Solidity(Lang):
    grammar, label, ext = "solidity", "Solidity", (".sol",)
    funcs = ("function_definition", "modifier_definition", "constructor_definition", "fallback_receive_definition")
    containers = types = ("contract_declaration", "interface_declaration", "library_declaration")
    calls, imports = ("call_expression",), ("import_directive",)

    def fn_name(self, n):
        if n.type == "constructor_definition":
            return "constructor"
        return super().fn_name(n)

    def param_nodes(self, n):
        return [c for c in n.named_children if c.type == "parameter"]

    def is_test_fn(self, n, name, test_file):
        return test_file and name.split(".")[-1].startswith("test")


class Fortran(Lang):
    grammar, label, ext = "fortran", "Fortran", (".f90", ".f95", ".f03", ".f08", ".f", ".for", ".f77")
    funcs, containers, calls = ("subroutine", "function"), ("module", "program"), ("subroutine_call", "call_expression")

    def _stmt(self, n):
        return next((c for c in n.named_children if c.type in ("subroutine_statement", "function_statement", "module_statement",
                                                               "program_statement")), None)

    def name_of(self, n):
        s = self._stmt(n)
        nm = (s.child_by_field_name("name") or next((c for c in s.named_children if c.type == "name"), None)) if s is not None else None
        return text_of(nm) if nm is not None else None

    def param_nodes(self, n):
        s = self._stmt(n)
        p = s.child_by_field_name("parameters") if s is not None else None
        return list(p.named_children) if p is not None else []

    def call_name(self, n):
        if n.type == "subroutine_call":
            s = n.child_by_field_name("subroutine")
            return text_of(s) if s is not None else None
        if n.type == "call_expression" and n.named_children:
            return _last(text_of(n.named_children[0]))
        return None


class Pascal(Lang):
    grammar, label, ext = "pascal", "Pascal", (".pas", ".dpr", ".lpr", ".dfm")
    funcs, calls, imports = ("defProc",), ("exprCall",), ("declUses",)

    def fn_name(self, n):
        if n.type != "defProc":
            return None
        h = n.child_by_field_name("header")
        nm = h.child_by_field_name("name") if h is not None else None
        return self.clean(text_of(nm)) if nm is not None else None

    def param_nodes(self, n):
        h = n.child_by_field_name("header")
        a = h.child_by_field_name("args") if h is not None else None
        return [c for c in a.named_children if c.type == "declArg"] if a is not None else []

    def call_name(self, n):
        if n.type != "exprCall":
            return None
        e = n.child_by_field_name("entity")
        return _last(text_of(e)) if e is not None else None


class Ada(Lang):
    grammar, label, ext = "ada", "Ada", (".adb", ".ads", ".ada")
    funcs, containers, calls, imports = ("subprogram_body",), ("package_body", "package_declaration"), ("function_call", "procedure_call_statement"), ("with_clause",)

    def _spec(self, n):
        return next((c for c in n.named_children if c.type in ("function_specification", "procedure_specification")), None)

    def fn_name(self, n):
        if n.type != "subprogram_body":
            return None
        s = self._spec(n)
        nm = s.child_by_field_name("name") if s is not None else None
        return self.clean(text_of(nm)) if nm is not None else None

    def param_nodes(self, n):
        s = self._spec(n)
        fp = next((c for c in s.named_children if c.type == "formal_part"), None) if s is not None else None
        return [c for c in fp.named_children if c.type == "parameter_specification"] if fp is not None else []

    def call_name(self, n):
        if n.type not in self.calls:
            return None
        nm = n.child_by_field_name("name")
        return _last(text_of(nm)) if nm is not None else (_last(text_of(n.named_children[0])) if n.named_children else None)


class D(Lang):
    grammar, label, ext = "d", "D", (".d", ".di")
    funcs, calls, imports = ("function_declaration",), ("call_expression",), ("import_declaration",)
    containers = types = ("class_declaration", "struct_declaration", "interface_declaration")

    def name_of(self, n):
        ident = next((c for c in n.named_children if c.type == "identifier"), None)
        return text_of(ident) if ident is not None else None

    def param_nodes(self, n):
        p = next((c for c in n.named_children if c.type == "parameters"), None)
        return [c for c in p.named_children if c.type == "parameter"] if p is not None else []

    def type_decl(self, n):
        if n.type not in self.types:
            return None
        name = self.name_of(n)
        bases = [text_of(x) for c in n.named_children if c.type in ("base_class", "base_interface_list", "base_class_list")
                 for x in walk(c) if x.type == "identifier"]
        return (name, bases, "class") if name else None

    def test_block(self, n):
        if n.type == "unittest_declaration":
            return f"unittest@{n.start_point[0] + 1}", n
        return None


class Gleam(Lang):
    grammar, label, ext = "gleam", "Gleam", (".gleam",)
    funcs, calls, imports, named_args = ("function",), ("function_call",), ("import",), True

    def param_nodes(self, n):
        p = n.child_by_field_name("parameters")
        return [c for c in p.named_children if c.type == "function_parameter"] if p is not None else []


class V(Lang):
    grammar, label, ext = "v", "V", (".v", ".vv", ".vsh")
    funcs, calls, imports, types = ("function_declaration",), ("call_expression",), ("import_declaration",), ("struct_declaration", "interface_declaration")

    def fn_name(self, n):
        name = super().fn_name(n)
        recv = n.child_by_field_name("receiver")
        if name and recv is not None:
            t = next((x for x in walk(recv) if x.type == "type_identifier"), None)
            return f"{text_of(t)}.{name}" if t is not None else name
        return name

    def param_nodes(self, n):
        p = n.child_by_field_name("parameters")
        return [c for c in p.named_children if c.type == "parameter_declaration"] if p is not None else []


class Odin(Lang):
    grammar, label, ext = "odin", "Odin", (".odin",)
    funcs, calls, imports = ("procedure_declaration",), ("call_expression",), ("import_declaration",)

    def param_nodes(self, n):
        proc = next((c for c in n.named_children if c.type == "procedure"), None)
        ps = next((c for c in proc.named_children if c.type == "parameters"), None) if proc is not None else None
        return [c for c in ps.named_children if c.type == "parameter"] if ps is not None else []


class Apex(Lang):
    grammar, label, ext = "apex", "Apex", (".cls", ".trigger")
    funcs = ("method_declaration", "constructor_declaration")
    containers = types = ("class_declaration", "interface_declaration", "enum_declaration")
    calls = ("method_invocation",)

    def is_test_fn(self, n, name, test_file):
        mods = next((c for c in n.named_children if c.type == "modifiers"), None)
        return mods is not None and re.search(r"(?i)@istest|\btestmethod\b", text_of(mods)) is not None

    def call_name(self, n):
        if n.type != "method_invocation":
            return None
        nm = n.child_by_field_name("name")
        return text_of(nm) if nm is not None else None


class Matlab(Lang):
    grammar, label, ext = "matlab", "MATLAB", (".m",)
    funcs, calls = ("function_definition",), ("function_call",)

    def param_nodes(self, n):
        a = next((c for c in n.named_children if c.type == "function_arguments"), None)
        return list(a.named_children) if a is not None else []


class Tcl(Lang):
    grammar, label, ext = "tcl", "Tcl", (".tcl", ".tm")
    funcs, calls, imports = ("procedure",), ("command",), ("command",)

    def call_name(self, n):
        if n.type != "command":
            return None
        c = n.child_by_field_name("name")
        name = text_of(c) if c is not None else ""
        return None if name in ("proc", "package", "set", "return", "if", "foreach", "while", "expr") else _last(name)

    def import_name(self, n):
        if n.type == "command":
            c = n.child_by_field_name("name")
            args = n.child_by_field_name("arguments")
            if c is not None and text_of(c) == "package" and args is not None and text_of(args).startswith("require"):
                return _last(text_of(args))
        return None

    def param_nodes(self, n):
        a = n.child_by_field_name("arguments")
        return [c for c in a.named_children if c.type == "argument"] if a is not None else []


LANGS = (Rust(), Ruby(), Php(), Hack(), C(), Cpp(), Header(), ObjC(), Scala(), Swift(), Dart(), Lua(), Elixir(), Erlang(), Haskell(), OCaml(),
         FSharp(), Elm(), R(), Julia(), Perl(), Bash(), PowerShell(), Clojure(), CommonLisp(), Scheme(), Racket(), Zig(), Nim(), Solidity(),
         Fortran(), Pascal(), Ada(), D(), Gleam(), V(), Odin(), Apex(), Matlab(), Tcl())
EXTENSIONS = tuple(sorted({e.lower() for lg in LANGS for e in lg.ext}))
LABELS = tuple(sorted({lg.label for lg in LANGS if lg.label != "C/C++ header"}))
_BY_EXT: dict = {}
for _lg in LANGS:
    for _e in _lg.ext:
        _BY_EXT.setdefault(_e.lower(), _lg)
_PARSERS: dict = {}


def language_for(path: str, text: str = "") -> Optional[Lang]:
    """The language of a file, from its extension and, where an extension is shared, its content."""
    p = path.lower()
    ext = posixpath.splitext(p)[1]
    if ext == ".m":                         # Objective-C or MATLAB
        return _by_name("objc") if re.search(r"^\s*(@interface|@implementation|#import|@protocol)", text, re.M) else _by_name("matlab")
    if ext == ".h" and re.search(r"^\s*(@interface|@protocol|#import)", text, re.M):
        return _by_name("objc")
    if ext == ".v" and re.search(r"\bendmodule\b", text):
        return None                         # Verilog, not V
    if ext == ".d" and re.match(r"\s*\S+\.o\s*:", text):
        return None                         # a make dependency file, not D source
    if ext == ".cls" and re.search(r"\\(ProvidesClass|NeedsTeXFormat|documentclass)", text):
        return None                         # a LaTeX class
    if ext == ".t" and not re.search(r"\buse\s+(Test::|strict)", text):
        return None
    return _BY_EXT.get(ext)


def _by_name(grammar: str) -> Lang:
    return next(lg for lg in LANGS if lg.grammar == grammar)


def _parser(grammar: str):
    if grammar not in _PARSERS:
        import tree_sitter_language_pack as tslp
        _PARSERS[grammar] = tslp.get_parser(grammar)
    return _PARSERS[grammar]


TEST_PATH = re.compile(r"(^|/)(tests?|spec|specs|__tests__|t)/|(^|/)test_[^/]+$|_(test|spec|tests)\.[a-z0-9]+$|(Test|Tests|Spec)\.[A-Za-z0-9]+$"
                       r"|\.(test|spec)\.[a-z0-9]+$|\.t\.sol$")


def is_test_file(path: str) -> bool:
    return TEST_PATH.search(path) is not None


def accepts(path: str) -> bool:
    from ..adapters import _not_generated
    return _not_generated(path)


def extract_file(repo: str, service: str, path: str, text: str, strip: tuple = ()):
    # tia.lang.common needs the package's EXTRACTOR_VERSION, which is computed from the adapter registry that lists this module's
    # extensions: import it when a file is extracted, not when the registry is built
    from .common import add_function, add_test, new_extract
    out = new_extract(repo, service)
    lg = language_for(path, text)
    if lg is None:
        out.non_python_files = 1
        return out
    data = text.encode("utf-8")
    if len(data) > MAX_BYTES:
        out.files_failed.append({"file": path, "error": f"{lg.label} file larger than {MAX_BYTES // (1024 * 1024)} MB: not parsed"})
        return out
    root = _parser(lg.grammar).parse(data).root_node
    test_file = is_test_file(path)
    imports: list = []
    funcs: dict = {}                       # qualname -> {node list, calls, sig, line, end}
    types: dict = {}
    tests: list = []
    skip_inside: set = set()

    def key(x):
        return (x.start_byte, x.end_byte, x.type)

    def scan_calls(nodes, skip=()) -> list:
        skip = {key(x) for x in skip}
        names, stack = [], [c for n in nodes for c in n.children]
        while stack:
            x = stack.pop()
            if key(x) in skip:
                continue
            if lg.fn_name(x) or (lg.type_decl(x) and x.type not in ("impl_item",)):
                continue                                   # a nested function or type has its own record
            nm = lg.call_name(x)
            if nm:
                names.append(nm)
            stack.extend(x.children)
        return names

    # iterative depth-first walk carrying the qualname prefix; prefix statements (Perl `package`, PHP `namespace X;`) set it for the
    # siblings that follow them
    stack = [(root, ())]
    while stack:
        n, prefix = stack.pop()
        imp = lg.import_name(n) if lg.imports and n.type in lg.imports else None
        if imp:
            imports.append(imp)
        td = lg.type_decl(n)
        if td:
            name, bases, kind = td
            qual = ".".join(prefix + (name,)) if n.type in lg.containers else name
            t = types.setdefault(name, {"bases": [], "line": n.start_point[0] + 1, "kind": kind, "qual": qual})
            t["bases"] = _uniq(t["bases"] + bases)
        tb = lg.test_block(n) if test_file or isinstance(lg, (Zig, D)) else None
        if tb:
            title, body = tb
            tests.append((title, n.start_point[0] + 1, scan_calls([body], [n] if body is not n else [])))
            continue
        fname = lg.fn_name(n)
        child_prefix = prefix
        if fname:
            nodes = lg.fn_nodes(n)
            qual = ".".join(prefix + (fname,))
            if nodes:
                heads = lg.head_nodes(n)
                if lg.is_test_fn(n, qual, test_file):
                    tests.append((fname.split(".")[-1], n.start_point[0] + 1, scan_calls(nodes, heads)))
                else:
                    f = funcs.setdefault(qual, {"nodes": [], "calls": [], "sig": lg.signature(n), "line": n.start_point[0] + 1, "end": 0})
                    f["nodes"] += nodes
                    f["calls"] += scan_calls(nodes, heads)
                    f["end"] = max(f["end"], max(x.end_point[0] + 1 for x in nodes))
            child_prefix = prefix + (fname.split(".")[-1],)
        else:
            cn = lg.container_name(n)
            if cn:
                child_prefix = prefix + (cn,)
        running, kids = child_prefix, []
        for c in n.children:
            if c.type in lg.prefix_statements and c.child_by_field_name("body") is None:
                nm = lg.name_of(c)
                running = child_prefix + ((nm,) if nm else ())
                continue
            kids.append((c, running))
        stack.extend(reversed(kids))

    if root.has_error and not funcs and not types and not tests:
        out.files_failed.append({"file": path, "error": f"{lg.label} source does not parse"})
        return out
    out.files_analysed = 1
    out.structure_only = {lg.label: 1}
    for qual, f in funcs.items():
        node = f["nodes"][0]
        add_function(out, repo, path, qual, node, f["calls"], f["sig"], f["line"])
        fn = out.functions[-1]
        fn.hash = fn.body_hash = _hash_nodes(f["nodes"])
        fn.end_line = f["end"]
    for name, t in types.items():
        out.types.append(TypeDecl(id=f"{repo}:{path}::{name}", repo=repo, service=service, name=name, file=path, line=t["line"],
                                  bases=t["bases"], kind=t["kind"]))
    seen: dict = {}
    for title, line, called in tests:
        q = title if title not in seen else f"{title}@{line}"
        seen[title] = True
        add_test(out, repo, path, q, title, line, imports, called, [])
    return out
