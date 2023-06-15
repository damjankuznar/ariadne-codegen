import io
import json
from typing import IO, Any, Dict, List, Optional, Tuple, TypeVar, cast

import httpx
from pydantic import BaseModel
from pydantic.json import pydantic_encoder

from .base_model import UNSET
from .exceptions import (
    GraphQLClientGraphQLMultiError,
    GraphQLClientHttpError,
    GraphQlClientInvalidResponseError,
)

Self = TypeVar("Self", bound="BaseClient")


class BaseClient:
    def __init__(
        self,
        url: str = "",
        headers: Optional[Dict[str, str]] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.url = url
        self.headers = headers

        self.http_client = http_client if http_client else httpx.Client(headers=headers)

    def __enter__(self: Self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_val: object,
        exc_tb: object,
    ) -> None:
        self.http_client.close()

    def execute(
        self, query: str, variables: Optional[Dict[str, Any]] = None
    ) -> httpx.Response:
        processed_variables, files_to_paths_map = self._process_variables(variables)
        payload: Dict[str, Any] = {"query": query, "variables": processed_variables}

        if files_to_paths_map:
            return self._execute_multipart(
                payload=payload,
                files_to_paths_map=files_to_paths_map,
            )

        return self._execute_json(payload=payload)

    def get_data(self, response: httpx.Response) -> dict[str, Any]:
        if not response.is_success:
            raise GraphQLClientHttpError(
                status_code=response.status_code, response=response
            )

        try:
            response_json = response.json()
        except ValueError as exc:
            raise GraphQlClientInvalidResponseError(response=response) from exc

        if (not isinstance(response_json, dict)) or ("data" not in response_json):
            raise GraphQlClientInvalidResponseError(response=response)

        data = response_json["data"]
        errors = response_json.get("errors")

        if errors:
            raise GraphQLClientGraphQLMultiError.from_errors_dicts(
                errors_dicts=errors, data=data
            )

        return cast(dict[str, Any], data)

    def _process_variables(
        self, variables: Optional[Dict[str, Any]]
    ) -> Tuple[Dict[str, Any], Dict[IO[bytes], List[str]]]:
        if not variables:
            return {}, {}

        serializable_variables = self._convert_dict_to_json_serializable(variables)
        return self._get_files_from_variables(serializable_variables)

    def _convert_dict_to_json_serializable(
        self, dict_: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {
            key: self._convert_value(value)
            for key, value in dict_.items()
            if value is not UNSET
        }

    def _convert_value(self, value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.dict(by_alias=True, exclude_unset=True)
        if isinstance(value, list):
            return [self._convert_value(item) for item in value]
        return value

    def _get_files_from_variables(
        self, variables: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[IO[bytes], List[str]]]:
        files_to_paths_map: Dict[IO[bytes], List[str]] = {}

        def separate_files(path: str, obj: Any) -> Any:
            if isinstance(obj, list):
                nulled_list = []
                for index, value in enumerate(obj):
                    value = separate_files(f"{path}.{index}", value)
                    nulled_list.append(value)
                return nulled_list

            if isinstance(obj, dict):
                nulled_dict = {}
                for key, value in obj.items():
                    value = separate_files(f"{path}.{key}", value)
                    nulled_dict[key] = value
                return nulled_dict

            if isinstance(obj, io.IOBase) and "b" in getattr(obj, "mode", "b"):
                checked_obj = cast(IO[bytes], obj)
                if checked_obj in files_to_paths_map:
                    files_to_paths_map[checked_obj].append(path)
                else:
                    files_to_paths_map[checked_obj] = [path]
                return None

            return obj

        nulled_variables = separate_files("variables", variables)
        return nulled_variables, files_to_paths_map

    def _execute_multipart(
        self,
        payload: Dict[str, Any],
        files_to_paths_map: Dict[IO[bytes], List[str]],
    ) -> httpx.Response:
        files_map: Dict[str, List[str]] = {
            str(i): files_to_paths_map[file_]
            for i, file_ in enumerate(files_to_paths_map.keys())
        }
        files: Dict[str, IO[bytes]] = {
            str(i): file_ for i, file_ in enumerate(files_to_paths_map.keys())
        }

        data = {
            "operations": json.dumps(payload, default=pydantic_encoder),
            "map": json.dumps(files_map, default=pydantic_encoder),
        }

        return self.http_client.post(url=self.url, data=data, files=files)

    def _execute_json(self, payload: Dict[str, Any]) -> httpx.Response:
        content = json.dumps(payload, default=pydantic_encoder)
        return self.http_client.post(
            url=self.url, content=content, headers={"Content-Type": "application/json"}
        )
