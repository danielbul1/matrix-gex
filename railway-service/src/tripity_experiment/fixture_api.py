from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

FIXTURE_TOKEN = "loop-one-secret-token"

app = FastAPI(
    title="Tripity Loop 1 Fixture API",
    version="1.0.0",
    description="A controlled API used only to validate generic OpenAPI conversion.",
)


def require_bearer(authorization: str | None = Header(default=None)) -> None:
    if authorization != f"Bearer {FIXTURE_TOKEN}":
        raise HTTPException(status_code=401, detail="Invalid bearer token")


class ItemInput(BaseModel):
    name: str
    quantity: int = 1
    metadata: dict[str, str] | None = None


class Item(ItemInput):
    id: int


@app.get("/items/{item_id}", operation_id="get_item", dependencies=[Depends(require_bearer)])
async def get_item(item_id: int, include_metadata: bool = False) -> Item:
    return Item(
        id=item_id,
        name=f"item-{item_id}",
        quantity=2,
        metadata={"source": "fixture"} if include_metadata else None,
    )


@app.post("/items", operation_id="create_item", dependencies=[Depends(require_bearer)])
async def create_item(item: ItemInput) -> Item:
    return Item(id=101, **item.model_dump())


@app.get("/private", operation_id="private_status", dependencies=[Depends(require_bearer)])
async def private_status() -> dict[str, str]:
    return {"status": "must-not-be-exposed"}


@app.get("/fail/{status_code}", operation_id="force_error", dependencies=[Depends(require_bearer)])
async def force_error(status_code: int) -> None:
    raise HTTPException(status_code=status_code, detail=f"Intentional {status_code} error")


@app.get("/slow", operation_id="slow_response", dependencies=[Depends(require_bearer)])
async def slow_response(delay_ms: int = 10) -> dict[str, int]:
    await asyncio.sleep(delay_ms / 1000)
    return {"delay_ms": delay_ms}

