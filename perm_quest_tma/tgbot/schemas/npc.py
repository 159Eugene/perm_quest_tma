from typing import Dict, List, Union, Any, Optional
from pydantic import BaseModel, Field, RootModel, ConfigDict


class DialogueOptionSchema(BaseModel):
    """
    Схема для конкретного варианта ответа в диалоге с NPC.
    """
    text: str
    next_node: str = "exit"
    karma_change: int = 0
    coins_change: int = 0
    xp_change: int = 0
    item_give: Optional[str] = None
    item_take: Optional[str] = None
    
    # --- Спринт 1: Поля для физического телепорта игрока на новые координаты ---
    target_lat: Optional[float] = None
    target_lng: Optional[float] = None


class DialogueNodeSchema(BaseModel):
    text: str
    options: List[DialogueOptionSchema] = Field(default_factory=list)


class NPCDialogueSchema(RootModel[Dict[str, DialogueNodeSchema]]):
    pass


class BranchTargetSchema(BaseModel):
    """Формализованная структура сложного перехода (ветки из condition_node)"""
    target: Union[int, str]
    fail_target: Optional[Union[int, str]] = None
    required_item: Optional[str] = None
    min_karma: int = 0
    min_level: int = 1
    required_class: Optional[str] = None
    target_lat: Optional[float] = None
    target_lng: Optional[float] = None
    target_radius: Optional[int] = None
    welcome_message: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class StepBranchesSchema(BaseModel):
    """
    Декларативная схема сюжетных переходов на шаге.
    """
    branches: Dict[str, Union[int, str, BranchTargetSchema, dict]] = Field(default_factory=dict)
    ui_meta: Optional[Dict[str, Any]] = Field(default=None, alias="_ui")

    model_config = ConfigDict(populate_by_name=True, extra="allow")