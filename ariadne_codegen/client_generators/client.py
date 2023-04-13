import ast
from typing import Dict, List, Optional, Union, cast

from graphql import OperationDefinitionNode

from ..codegen import (
    generate_ann_assign,
    generate_arg,
    generate_arguments,
    generate_assign,
    generate_async_method_definition,
    generate_attribute,
    generate_await,
    generate_call,
    generate_class_def,
    generate_constant,
    generate_import_from,
    generate_keyword,
    generate_method_definition,
    generate_module,
    generate_name,
    generate_return,
    generate_subscript,
    generate_tuple,
)
from ..plugins.manager import PluginManager
from .arguments import ArgumentsGenerator
from .constants import ANY, LIST, OPTIONAL, TYPING_MODULE, UNION
from .scalars import ScalarData, generate_scalar_imports


class ClientGenerator:
    def __init__(
        self,
        name: str,
        base_client: str,
        enums_module_name: str,
        input_types_module_name: str,
        arguments_generator: ArgumentsGenerator,
        base_client_import: Optional[ast.ImportFrom] = None,
        unset_import: Optional[ast.ImportFrom] = None,
        custom_scalars: Optional[Dict[str, ScalarData]] = None,
        plugin_manager: Optional[PluginManager] = None,
    ) -> None:
        self.name = name
        self.enums_module_name = enums_module_name
        self.input_types_module_name = input_types_module_name
        self.plugin_manager = plugin_manager
        self.custom_scalars = custom_scalars if custom_scalars else {}
        self.arguments_generator = arguments_generator

        self._imports: List[ast.ImportFrom] = []
        self._add_import(
            generate_import_from([OPTIONAL, LIST, ANY, UNION], TYPING_MODULE)
        )
        self._add_import(base_client_import)
        self._add_import(unset_import)

        self._class_def = generate_class_def(name=name, base_names=[base_client])
        self._gql_func_name = "gql"
        self._operation_str_variable = "query"
        self._variables_dict_variable = "variables"
        self._response_variable = "response"
        self._data_variable = "data"

    def generate(self) -> ast.Module:
        """Generate module with class definition of graphql client."""
        self._add_import(
            generate_import_from(
                names=self.arguments_generator.get_used_inputs(),
                from_=self.input_types_module_name,
                level=1,
            )
        )
        self._add_import(
            generate_import_from(
                names=self.arguments_generator.get_used_enums(),
                from_=self.enums_module_name,
                level=1,
            )
        )
        for custom_scalar_name in self.arguments_generator.get_used_custom_scalars():
            scalar_data = self.custom_scalars[custom_scalar_name]
            for import_ in generate_scalar_imports(scalar_data):
                self._add_import(import_)

        gql_func = self._generate_gql_func()
        gql_func.lineno = len(self._imports) + 1
        if self.plugin_manager:
            gql_func = self.plugin_manager.generate_gql_function(gql_func)

        self._class_def.lineno = len(self._imports) + 3
        if not self._class_def.body:
            self._class_def.body.append(ast.Pass())
        if self.plugin_manager:
            self._class_def = self.plugin_manager.generate_client_class(self._class_def)

        module = generate_module(
            body=self._imports + [gql_func, self._class_def],
        )
        if self.plugin_manager:
            module = self.plugin_manager.generate_client_module(module)
        return module

    def add_method(
        self,
        definition: OperationDefinitionNode,
        name: str,
        return_type: str,
        return_type_module: str,
        operation_str: str,
        async_: bool = True,
    ):
        """Add method to client."""
        arguments, arguments_dict = self.arguments_generator.generate(
            definition.variable_definitions
        )
        method_def = (
            self._generate_async_method(
                name=name,
                return_type=return_type,
                arguments=arguments,
                arguments_dict=arguments_dict,
                operation_str=operation_str,
            )
            if async_
            else self._generate_method(
                name=name,
                return_type=return_type,
                arguments=arguments,
                arguments_dict=arguments_dict,
                operation_str=operation_str,
            )
        )
        method_def.lineno = len(self._class_def.body) + 1
        if self.plugin_manager:
            method_def = self.plugin_manager.generate_client_method(
                cast(Union[ast.FunctionDef, ast.AsyncFunctionDef], method_def)
            )
        self._class_def.body.append(method_def)
        self._add_import(
            generate_import_from(names=[return_type], from_=return_type_module, level=1)
        )

    def _add_import(self, import_: Optional[ast.ImportFrom] = None):
        if not import_:
            return
        if self.plugin_manager:
            import_ = self.plugin_manager.generate_client_import(import_)
        if import_.names and import_.module:
            self._imports.append(import_)

    def _generate_async_method(
        self,
        name: str,
        return_type: str,
        arguments: ast.arguments,
        arguments_dict: ast.Dict,
        operation_str: str,
    ) -> ast.AsyncFunctionDef:
        return generate_async_method_definition(
            name=name,
            arguments=arguments,
            return_type=generate_name(return_type),
            body=[
                self._generate_operation_str_assign(operation_str, 1),
                self._generate_variables_assign(arguments_dict, 2),
                self._generate_async_response_assign(3),
                self._generate_data_retrieval(),
                self._generate_return_parsed_obj(return_type),
            ],
        )

    def _generate_method(
        self,
        name: str,
        return_type: str,
        arguments: ast.arguments,
        arguments_dict: ast.Dict,
        operation_str: str,
    ) -> ast.FunctionDef:
        return generate_method_definition(
            name=name,
            arguments=arguments,
            return_type=generate_name(return_type),
            body=[
                self._generate_operation_str_assign(operation_str, 1),
                self._generate_variables_assign(arguments_dict, 2),
                self._generate_response_assign(3),
                self._generate_data_retrieval(),
                self._generate_return_parsed_obj(return_type),
            ],
        )

    def _generate_operation_str_assign(
        self, operation_str: str, lineno: int = 1
    ) -> ast.Assign:
        return generate_assign(
            targets=[self._operation_str_variable],
            value=generate_call(
                func=generate_name(self._gql_func_name),
                args=[
                    [generate_constant(l + "\n") for l in operation_str.splitlines()]
                ],
            ),
            lineno=lineno,
        )

    def _generate_variables_assign(
        self, arguments_dict: ast.Dict, lineno: int = 1
    ) -> ast.AnnAssign:
        return generate_ann_assign(
            target=self._variables_dict_variable,
            annotation=generate_subscript(
                generate_name("dict"),
                generate_tuple([generate_name("str"), generate_name("object")]),
            ),
            value=arguments_dict,
            lineno=lineno,
        )

    def _generate_async_response_assign(self, lineno: int = 1) -> ast.Assign:
        return generate_assign(
            targets=[self._response_variable],
            value=generate_await(self._generate_execute_call()),
            lineno=lineno,
        )

    def _generate_response_assign(self, lineno: int = 1) -> ast.Assign:
        return generate_assign(
            targets=[self._response_variable],
            value=self._generate_execute_call(),
            lineno=lineno,
        )

    def _generate_execute_call(self) -> ast.Call:
        return generate_call(
            func=generate_attribute(generate_name("self"), "execute"),
            keywords=[
                generate_keyword("query", generate_name(self._operation_str_variable)),
                generate_keyword(
                    "variables", generate_name(self._variables_dict_variable)
                ),
            ],
        )

    def _generate_data_retrieval(self) -> ast.Assign:
        return generate_assign(
            targets=[self._data_variable],
            value=generate_call(
                func=generate_attribute(value=generate_name("self"), attr="get_data"),
                args=[generate_name(self._response_variable)],
            ),
        )

    def _generate_return_parsed_obj(self, return_type: str) -> ast.Return:
        return generate_return(
            generate_call(
                func=generate_attribute(generate_name(return_type), "parse_obj"),
                args=[generate_name(self._data_variable)],
            )
        )

    def _generate_gql_func(self) -> ast.FunctionDef:
        str_name = generate_name("str")
        arg = "q"
        return generate_method_definition(
            name=self._gql_func_name,
            arguments=generate_arguments([generate_arg(arg, str_name)]),
            return_type=str_name,
            body=[generate_return(generate_name(arg))],
        )