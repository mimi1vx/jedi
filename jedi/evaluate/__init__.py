"""
Evaluation of Python code in |jedi| is based on three assumptions:

* The code uses as least side effects as possible. Jedi understands certain
  list/tuple/set modifications, but there's no guarantee that Jedi detects
  everything (list.append in different modules for example).
* No magic is being used:

  - metaclasses
  - ``setattr()`` / ``__import__()``
  - writing to ``globals()``, ``locals()``, ``object.__dict__``
* The programmer is not a total dick, e.g. like `this
  <https://github.com/davidhalter/jedi/issues/24>`_ :-)

The actual algorithm is based on a principle called lazy evaluation. If you
don't know about it, google it.  That said, the typical entry point for static
analysis is calling ``eval_statement``. There's separate logic for
autocompletion in the API, the evaluator is all about evaluating an expression.

Now you need to understand what follows after ``eval_statement``. Let's
make an example::

    import datetime
    datetime.date.toda# <-- cursor here

First of all, this module doesn't care about completion. It really just cares
about ``datetime.date``. At the end of the procedure ``eval_statement`` will
return the ``date`` class.

To *visualize* this (simplified):

- ``Evaluator.eval_statement`` doesn't do much, because there's no assignment.
- ``Evaluator.eval_element`` cares for resolving the dotted path
- ``Evaluator.find_types`` searches for global definitions of datetime, which
  it finds in the definition of an import, by scanning the syntax tree.
- Using the import logic, the datetime module is found.
- Now ``find_types`` is called again by ``eval_element`` to find ``date``
  inside the datetime module.

Now what would happen if we wanted ``datetime.date.foo.bar``? Two more
calls to ``find_types``. However the second call would be ignored, because the
first one would return nothing (there's no foo attribute in ``date``).

What if the import would contain another ``ExprStmt`` like this::

    from foo import bar
    Date = bar.baz

Well... You get it. Just another ``eval_statement`` recursion. It's really
easy. Python can obviously get way more complicated then this. To understand
tuple assignments, list comprehensions and everything else, a lot more code had
to be written.

Jedi has been tested very well, so you can just start modifying code. It's best
to write your own test first for your "new" feature. Don't be scared of
breaking stuff. As long as the tests pass, you're most likely to be fine.

I need to mention now that lazy evaluation is really good because it
only *evaluates* what needs to be *evaluated*. All the statements and modules
that are not used are just being ignored.
"""

import copy
import sys
from itertools import chain

from jedi.parser import tree
from jedi import debug
from jedi.common import unite
from jedi.evaluate import representation as er
from jedi.evaluate import imports
from jedi.evaluate import recursion
from jedi.evaluate import iterable
from jedi.evaluate.cache import memoize_default
from jedi.evaluate import stdlib
from jedi.evaluate import finder
from jedi.evaluate import compiled
from jedi.evaluate import precedence
from jedi.evaluate import param
from jedi.evaluate import helpers
from jedi.evaluate.filters import TreeNameDefinition
from jedi.evaluate.instance import AnonymousInstance, AnonymousInstanceFunctionExecution


class Evaluator(object):
    def __init__(self, grammar, sys_path=None):
        self.grammar = grammar
        self.memoize_cache = {}  # for memoize decorators
        # To memorize modules -> equals `sys.modules`.
        self.modules = {}  # like `sys.modules`.
        self.compiled_cache = {}  # see `evaluate.compiled.create()`
        self.mixed_cache = {}  # see `evaluate.compiled.mixed.create()`
        self.analysis = []
        self.predefined_if_name_dict_dict = {}
        self.dynamic_params_depth = 0
        self.is_analysis = False

        if sys_path is None:
            sys_path = sys.path
        self.sys_path = copy.copy(sys_path)
        try:
            self.sys_path.remove('')
        except ValueError:
            pass

        self.reset_recursion_limitations()

        # Constants
        self.BUILTINS = compiled.get_special_object(self, 'BUILTINS')

    def reset_recursion_limitations(self):
        self.recursion_detector = recursion.RecursionDetector(self)
        self.execution_recursion_detector = recursion.ExecutionRecursionDetector(self)

    def find_types(self, context, name_str, position=None, search_global=False,
                   is_goto=False):
        """
        This is the search function. The most important part to debug.
        `remove_statements` and `filter_statements` really are the core part of
        this completion.

        :param position: Position of the last statement -> tuple of line, column
        :return: List of Names. Their parents are the types.
        """
        f = finder.NameFinder(self, context, name_str, position)
        filters = f.get_filters(search_global)
        if is_goto:
            return f.filter_name(filters)
        return f.find(filters, attribute_lookup=not search_global)

    #@memoize_default(default=[], evaluator_is_first_arg=True)
    #@recursion.recursion_decorator
    @debug.increase_indent
    def eval_statement(self, context, stmt, seek_name=None):
        """
        The starting point of the completion. A statement always owns a call
        list, which are the calls, that a statement does. In case multiple
        names are defined in the statement, `seek_name` returns the result for
        this name.

        :param stmt: A `tree.ExprStmt`.
        """
        debug.dbg('eval_statement %s (%s)', stmt, seek_name)
        rhs = stmt.get_rhs()
        types = self.eval_element(context, rhs)

        if seek_name:
            types = finder.check_tuple_assignments(self, types, seek_name)

        first_operation = stmt.first_operation()
        if first_operation not in ('=', None) and not isinstance(stmt, er.InstanceElement):  # TODO don't check for this.
            # `=` is always the last character in aug assignments -> -1
            operator = copy.copy(first_operation)
            operator.value = operator.value[:-1]
            name = str(stmt.get_defined_names()[0])
            left = self.find_types(context, name, stmt.start_pos, search_global=True)

            for_stmt = stmt.get_parent_until(tree.ForStmt)
            if isinstance(for_stmt, tree.ForStmt) and types \
                    and for_stmt.defines_one_name():
                # Iterate through result and add the values, that's possible
                # only in for loops without clutter, because they are
                # predictable. Also only do it, if the variable is not a tuple.
                node = for_stmt.get_input_node()
                for_iterables = self.eval_element(context, node)
                ordered = list(iterable.py__iter__(self, for_iterables, node))

                for index_types in ordered:
                    dct = {str(for_stmt.children[1]): index_types}
                    self.predefined_if_name_dict_dict[for_stmt] = dct
                    t = self.eval_element(context, rhs)
                    left = precedence.calculate(self, left, operator, t)
                types = left
                if ordered:
                    # If there are no for entries, we cannot iterate and the
                    # types are defined by += entries. Therefore the for loop
                    # is never called.
                    del self.predefined_if_name_dict_dict[for_stmt]
            else:
                types = precedence.calculate(self, left, operator, types)
        debug.dbg('eval_statement result %s', types)
        return types

    def eval_element(self, context, element):
        if_stmt = element.get_parent_until((tree.IfStmt, tree.ForStmt, tree.IsScope))
        predefined_if_name_dict = self.predefined_if_name_dict_dict.get(if_stmt)
        if predefined_if_name_dict is None and isinstance(if_stmt, tree.IfStmt):
            if_stmt_test = if_stmt.children[1]
            name_dicts = [{}]
            # If we already did a check, we don't want to do it again -> If
            # predefined_if_name_dict_dict is filled, we stop.
            # We don't want to check the if stmt itself, it's just about
            # the content.
            if element.start_pos > if_stmt_test.end_pos:
                # Now we need to check if the names in the if_stmt match the
                # names in the suite.
                if_names = helpers.get_names_of_node(if_stmt_test)
                element_names = helpers.get_names_of_node(element)
                str_element_names = [str(e) for e in element_names]
                if any(str(i) in str_element_names for i in if_names):
                    for if_name in if_names:
                        definitions = self.goto_definitions(context, if_name)
                        # Every name that has multiple different definitions
                        # causes the complexity to rise. The complexity should
                        # never fall below 1.
                        if len(definitions) > 1:
                            if len(name_dicts) * len(definitions) > 16:
                                debug.dbg('Too many options for if branch evaluation %s.', if_stmt)
                                # There's only a certain amount of branches
                                # Jedi can evaluate, otherwise it will take to
                                # long.
                                name_dicts = [{}]
                                break

                            original_name_dicts = list(name_dicts)
                            name_dicts = []
                            for definition in definitions:
                                new_name_dicts = list(original_name_dicts)
                                for i, name_dict in enumerate(new_name_dicts):
                                    new_name_dicts[i] = name_dict.copy()
                                    new_name_dicts[i][str(if_name)] = [definition]

                                name_dicts += new_name_dicts
                        else:
                            for name_dict in name_dicts:
                                name_dict[str(if_name)] = definitions
            if len(name_dicts) > 1:
                result = set()
                for name_dict in name_dicts:
                    self.predefined_if_name_dict_dict[if_stmt] = name_dict
                    try:
                        result |= self._eval_element_not_cached(context, element)
                    finally:
                        del self.predefined_if_name_dict_dict[if_stmt]
                return result
            else:
                return self._eval_element_if_evaluated(context, element)
                return self._eval_element_cached(context, element)
        else:
            if predefined_if_name_dict:
                return self._eval_element_not_cached(context, element)
            else:
                return self._eval_element_if_evaluated(context, element)
                return self._eval_element_cached(context, element)

    def _eval_element_if_evaluated(self, context, element):
        """
        TODO This function is temporary: Merge with eval_element.
        """
        parent = element
        while parent is not None:
            parent = parent.parent
            predefined_if_name_dict = self.predefined_if_name_dict_dict.get(parent)
            if predefined_if_name_dict is not None:
                return self._eval_element_not_cached(context, element)
        return self._eval_element_cached(context, element)

    @memoize_default(default=set(), evaluator_is_first_arg=True)
    def _eval_element_cached(self, context, element):
        return self._eval_element_not_cached(context, element)

    @debug.increase_indent
    def _eval_element_not_cached(self, context, element):
        debug.dbg('eval_element %s@%s', element, element.start_pos)
        types = set()
        if isinstance(element, (tree.Name, tree.Literal)) or tree.is_node(element, 'atom'):
            types = self._eval_atom(context, element)
        elif isinstance(element, tree.Keyword):
            # For False/True/None
            if element.value in ('False', 'True', 'None'):
                types.add(compiled.builtin_from_name(self, element.value))
            # else: print e.g. could be evaluated like this in Python 2.7
        elif isinstance(element, tree.Lambda):
            types = set([er.LambdaWrapper(self, context, element)])
        elif element.isinstance(er.LambdaWrapper):
            types = set([element])  # TODO this is no real evaluation.
        elif element.type == 'expr_stmt':
            types = self.eval_statement(context, element)
        elif element.type in ('power', 'atom_expr'):
            types = self._eval_atom(context, element.children[0])
            for trailer in element.children[1:]:
                if trailer == '**':  # has a power operation.
                    right = self.eval_element(context, element.children[2])
                    types = set(precedence.calculate(self, types, trailer, right))
                    break
                types = self.eval_trailer(context, types, trailer)
        elif element.type in ('testlist_star_expr', 'testlist',):
            # The implicit tuple in statements.
            types = set([iterable.ArrayLiteralContext(self, context, element)])
        elif element.type in ('not_test', 'factor'):
            types = self.eval_element(context, element.children[-1])
            for operator in element.children[:-1]:
                types = set(precedence.factor_calculate(self, types, operator))
        elif element.type == 'test':
            # `x if foo else y` case.
            types = (self.eval_element(context, element.children[0]) |
                     self.eval_element(context, element.children[-1]))
        elif element.type == 'operator':
            # Must be an ellipsis, other operators are not evaluated.
            assert element.value == '...'
            types = set([compiled.create(self, Ellipsis)])
        elif element.type == 'dotted_name':
            types = self._eval_atom(context, element.children[0])
            for next_name in element.children[2::2]:
                # TODO add search_global=True?
                types = set(chain.from_iterable(self.find_types(typ, next_name)
                                                for typ in types))
            types = types
        elif element.type == 'eval_input':
            types = self._eval_element_not_cached(context, element.children[0])
        else:
            types = precedence.calculate_children(self, context, element.children)
        debug.dbg('eval_element result %s', types)
        return types

    def _eval_atom(self, context, atom):
        """
        Basically to process ``atom`` nodes. The parser sometimes doesn't
        generate the node (because it has just one child). In that case an atom
        might be a name or a literal as well.
        """
        if isinstance(atom, tree.Name):
            # This is the first global lookup.
            stmt = atom.get_definition()
            #if isinstance(context, er.FunctionExecution):
                ## Adjust scope: If the name is not in the suite, it's a param
                ## default or annotation and will be resolved as part of the
                ## parent scope.
                #colon = scope.children.index(':')
                #if atom.start_pos < scope.children[colon + 1].start_pos:
                    ##scope = scope.get_parent_scope()
            if isinstance(stmt, tree.CompFor):
                stmt = stmt.get_parent_until((tree.ClassOrFunc, tree.ExprStmt))
            if stmt.type != 'expr_stmt':
                # We only need to adjust the start_pos for statements, because
                # there the name cannot be used.
                stmt = atom
            return self.find_types(context, atom, stmt.start_pos, search_global=True)
        elif isinstance(atom, tree.Literal):
            return set([compiled.create(self, atom.eval())])
        else:
            c = atom.children
            if c[0].type == 'string':
                # Will be one string.
                types = self._eval_atom(context, c[0])
                for string in c[1:]:
                    right = self._eval_atom(context, string)
                    types = precedence.calculate(self, types, '+', right)
                return types
            # Parentheses without commas are not tuples.
            elif c[0] == '(' and not len(c) == 2 \
                    and not(tree.is_node(c[1], 'testlist_comp')
                            and len(c[1].children) > 1):
                return self.eval_element(context, c[1])

            try:
                comp_for = c[1].children[1]
            except (IndexError, AttributeError):
                pass
            else:
                if comp_for == ':':
                    # Dict comprehensions have a colon at the 3rd index.
                    try:
                        comp_for = c[1].children[3]
                    except IndexError:
                        pass

                if comp_for.type == 'comp_for':
                    return set([iterable.Comprehension.from_atom(self, context, atom)])
            return set([iterable.ArrayLiteralContext(self, context, atom)])

    def eval_trailer(self, context, types, trailer):
        trailer_op, node = trailer.children[:2]
        if node == ')':  # `arglist` is optional.
            node = ()

        new_types = set()
        if trailer_op == '[':
            new_types |= iterable.py__getitem__(self, context, types, trailer)
        else:
            for typ in types:
                debug.dbg('eval_trailer: %s in scope %s', trailer, typ)
                if trailer_op == '.':
                    new_types |= self.find_types(typ, node)
                elif trailer_op == '(':
                    arguments = param.TreeArguments(self, context, node, trailer)
                    new_types |= self.execute(typ, arguments)
        return new_types

    @debug.increase_indent
    def execute(self, obj, arguments):
        if not isinstance(arguments, param.AbstractArguments):
            raise NotImplementedError
            arguments = param.Arguments(self, arguments)

        if self.is_analysis:
            arguments.eval_all()

        debug.dbg('execute: %s %s', obj, arguments)
        try:
            # Some stdlib functions like super(), namedtuple(), etc. have been
            # hard-coded in Jedi to support them.
            return stdlib.execute(self, obj, arguments)
        except stdlib.NotInStdLib:
            pass

        try:
            func = obj.py__call__
        except AttributeError:
            debug.warning("no execution possible %s", obj)
            return set()
        else:
            types = func(arguments)
            debug.dbg('execute result: %s in %s', types, obj)
            return types

    def goto_definitions(self, context, name):
        def_ = name.get_definition()
        is_simple_name = name.parent.type not in ('power', 'trailer')
        if is_simple_name:
            if name.parent.type in ('file_input', 'classdef', 'funcdef'):
                return [self.wrap(name.parent)]
            if def_.type == 'expr_stmt' and name in def_.get_defined_names():
                return self.eval_statement(context, def_, name)
            elif def_.type == 'for_stmt':
                container_types = self.eval_element(context, def_.children[3])
                for_types = iterable.py__iter__types(self, container_types, def_.children[3])
                return finder.check_tuple_assignments(self, for_types, name)
            elif def_.type in ('import_from', 'import_name'):
                return imports.ImportWrapper(context, name).follow()

        call = helpers.call_of_leaf(name)
        return self.eval_element(context, call)

    def goto(self, context, name):
        def resolve_implicit_imports(names):
            for name in names:
                if isinstance(name.parent, helpers.FakeImport):
                    # Those are implicit imports.
                    s = imports.ImportWrapper(context, name)
                    for n in s.follow(is_goto=True):
                        yield n
                else:
                    yield name

        stmt = name.get_definition()
        par = name.parent
        if par.type == 'argument' and par.children[1] == '=' and par.children[0] == name:
            # Named param goto.
            trailer = par.parent
            if trailer.type == 'arglist':
                trailer = trailer.parent
            if trailer.type != 'classdef':
                if trailer.type == 'decorator':
                    types = self.eval_element(context, trailer.children[1])
                else:
                    i = trailer.parent.children.index(trailer)
                    to_evaluate = trailer.parent.children[:i]
                    types = self.eval_element(context, to_evaluate[0])
                    for trailer in to_evaluate[1:]:
                        types = self.eval_trailer(context, types, trailer)
                param_names = []
                for typ in types:
                    try:
                        get_param_names = typ.get_param_names
                    except AttributeError:
                        pass
                    else:
                        for param_name in get_param_names():
                            if param_name.string_name == name.value:
                                param_names.append(param_name)
                return param_names
        elif isinstance(par, tree.ExprStmt) and name in par.get_defined_names():
            # Only take the parent, because if it's more complicated than just
            # a name it's something you can "goto" again.
            return [TreeNameDefinition(context, name)]
        elif isinstance(par, (tree.Param, tree.Function, tree.Class)) and par.name is name:
            return [TreeNameDefinition(context, name)]
        elif isinstance(stmt, tree.Import):
            module_names = imports.ImportWrapper(context, name).follow(is_goto=True)
            return module_names
            return list(resolve_implicit_imports(module_names))
        elif par.type == 'dotted_name':  # Is a decorator.
            index = par.children.index(name)
            if index > 0:
                new_dotted = helpers.deep_ast_copy(par)
                new_dotted.children[index - 1:] = []
                values = self.eval_element(context, new_dotted)
                return unite(
                    self.find_types(value, name, is_goto=True) for value in values
                )
                return resolve_implicit_imports(iterable.unite(
                    self.find_types(typ, name, is_goto=True) for typ in types
                ))

        if tree.is_node(par, 'trailer') and par.children[0] == '.':
            call = helpers.call_of_leaf(name, cut_own_trailer=True)
            values = self.eval_element(context, call)
            return unite(
                self.find_types(value, name, is_goto=True) for value in values
            )
            return resolve_implicit_imports(iterable.unite(
                self.find_types(typ, name, is_goto=True) for typ in types
            ))
        else:
            if stmt.type != 'expr_stmt':
                # We only need to adjust the start_pos for statements, because
                # there the name cannot be used.
                stmt = name
            return self.find_types(context, name, stmt.start_pos,
                                   search_global=True, is_goto=True)

    def wrap(self, element, parent_context):
        raise DeprecationWarning
        if element.type == 'classdef':
            return er.ClassContext(self, element, parent_context)
        elif element.type == 'lambda':
            return er.LambdaWrapper(self, element)
        elif element.type == 'file_input':
            return er.ModuleContext(self, element)
        else:
            raise DeprecationWarning
            return element

    def create_context(self, module_context, node):
        def from_scope_node(scope_node, child_is_funcdef=None):
            is_funcdef = scope_node.type == 'funcdef'
            parent_context = None
            parent_scope = scope_node.get_parent_scope()
            if parent_scope is not None:
                parent_context = from_scope_node(parent_scope, child_is_funcdef=is_funcdef)

            # TODO this whole procedure just ignores decorators
            if scope_node == module_context.module_node:
                return module_context
            elif is_funcdef:
                if isinstance(parent_context, AnonymousInstance):
                    return AnonymousInstanceFunctionExecution(
                        parent_context,
                        parent_context.parent_context,
                        scope_node
                    )
                else:
                    return er.AnonymousFunctionExecution(self, parent_context, scope_node)
            elif scope_node.type == 'classdef':
                class_context = er.ClassContext(self, scope_node, parent_context)
                if child_is_funcdef:
                    # anonymous instance
                    return AnonymousInstance(self, parent_context, class_context)
                else:
                    return class_context

        if node.is_scope():
            scope_node = node
        else:
            scope_node = node.get_parent_scope()
        return from_scope_node(scope_node)
