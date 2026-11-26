"""MCP server: the shop's customer tools for any MCP client, acting as one signed-in customer.

The tools are the ones the chat agent uses (`create_tools`), with the same input schemas, the same access checks and
the same refusals; nothing here re-implements a business rule.

No tool changes an order. `propose_cancel_order` and `propose_return` only create a proposal card with a preview and
an expiry, exactly as in the chat. The change happens when the customer confirms the card, which is their own
signed-in request to `POST /actions/{action_id}/confirm` (the card's button in the web app). This server exposes no
confirm tool and never calls `ActionService.confirm_action`, so an MCP client, or the model driving it, cannot carry
out a cancellation or a return by itself.

Each server process works in one conversation of its own, created at start-up: proposals and support tickets are
attached to it, as they are to a chat conversation.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from langchain_core.tools import StructuredTool
from mcp import types
from mcp.server.lowlevel import Server
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shopagent.agent.tools.definitions import create_tools
from shopagent.core.principal import Principal
from shopagent.core.security import verify_password
from shopagent.infra.db.tables import ConversationModel, CustomerCredentialsModel, CustomerModel

SERVER_NAME = "shopagent"

# Tools that only read. The two propose tools write a proposal card (not the order) and return the same card when
# called again with the same input; escalate_to_human writes a support ticket.
READ_ONLY_TOOLS = frozenset(
    {
        "search_policy",
        "list_my_orders",
        "get_order",
        "track_shipment",
        "check_return_eligibility",
        "check_variant_stock",
        "get_case_status",
    }
)
PROPOSAL_TOOLS = frozenset({"propose_cancel_order", "propose_return"})
EXPOSED_TOOLS = READ_ONLY_TOOLS | PROPOSAL_TOOLS | {"escalate_to_human"}

INSTRUCTIONS = (
    "Công cụ chăm sóc khách hàng của cửa hàng, thay mặt một khách hàng đã đăng nhập. "
    "Các công cụ propose_* chỉ tạo thẻ đề xuất; đơn hàng chỉ thay đổi khi chính khách bấm Xác nhận trên thẻ "
    "trong ứng dụng của cửa hàng. Không có công cụ nào để xác nhận thay khách."
)


class AuthenticationError(Exception):
    """The configured credentials do not belong to a customer account."""


async def authenticate_customer(factory: async_sessionmaker[AsyncSession], email: str, password: str) -> Principal:
    """The customer behind these credentials, checked the way `POST /auth/login` checks them."""
    async with factory() as session:
        row = (
            await session.execute(
                select(CustomerModel, CustomerCredentialsModel)
                .join(CustomerCredentialsModel, CustomerModel.id == CustomerCredentialsModel.customer_id)
                .where(CustomerModel.email == email.lower().strip())
            )
        ).first()
    if row is None or not verify_password(password, row[1].password_hash):
        raise AuthenticationError("Email hoặc mật khẩu không chính xác.")
    return Principal.customer(customer_id=row[0].id, email=row[0].email)


async def open_conversation(factory: async_sessionmaker[AsyncSession], principal: Principal) -> uuid.UUID:
    """A new conversation owned by the customer, to hold this server's proposals and tickets."""
    conversation_id = uuid.uuid4()
    async with factory() as session:
        session.add(ConversationModel(id=conversation_id, customer_id=principal.customer_id, mode="ai"))
        await session.commit()
    return conversation_id


def _annotations(name: str) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        read_only_hint=name in READ_ONLY_TOOLS,
        destructive_hint=False,
        idempotent_hint=name in READ_ONLY_TOOLS or name in PROPOSAL_TOOLS,
        open_world_hint=False,
    )


def _input_schema(tool: StructuredTool) -> dict[str, Any]:
    schema = tool.args_schema
    if isinstance(schema, dict):
        return schema
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_json_schema()
    return {"type": "object", "properties": {}}


def _result(payload: dict[str, Any], *, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, default=str))],
        structured_content=payload,
        is_error=is_error,
    )


def build_server(
    factory: async_sessionmaker[AsyncSession],
    principal: Principal,
    conversation_id: uuid.UUID,
) -> Server[Any]:
    """The MCP server for one customer and one conversation."""
    if not principal.is_authenticated_customer:
        raise AuthenticationError("Máy chủ MCP chỉ phục vụ tài khoản khách hàng.")

    def tools_for(session: AsyncSession) -> dict[str, StructuredTool]:
        tools = create_tools(session=session, principal=principal, conversation_id=conversation_id)
        return {tool.name: tool for tool in tools if tool.name in EXPOSED_TOOLS}

    async def list_tools(_ctx: Any, _params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        async with factory() as session:  # building the tools runs no query
            tools = tools_for(session)
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=tool.name,
                    description=tool.description,
                    input_schema=_input_schema(tool),
                    annotations=_annotations(tool.name),
                )
                for tool in tools.values()
            ]
        )

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        async with factory() as session:
            tool = tools_for(session).get(params.name)
            if tool is None:
                return _result({"error_code": "UNKNOWN_TOOL", "message": f"Không có công cụ '{params.name}'."}, is_error=True)
            try:
                output = await tool.ainvoke(params.arguments or {})
            except ValidationError as e:
                await session.rollback()
                return _result({"error_code": "INVALID_ARGUMENTS", "message": str(e)}, is_error=True)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
        payload = output if isinstance(output, dict) else {"result": output}
        # A domain refusal (order not found, not cancellable, ...) is an answer the model must see as a failed call.
        return _result(payload, is_error="error_code" in payload)

    return Server(
        SERVER_NAME,
        version="1.0.0",
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
