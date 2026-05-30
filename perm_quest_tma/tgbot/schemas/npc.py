from typing import Dict, List, Union
from pydantic import BaseModel, Field, RootModel


class DialogueOptionSchema(BaseModel):
    """
    Схема для конкретного варианта ответа в диалоге с NPC.
    Определяет текст кнопки, следующий узел перехода и RPG-эффекты выбора.
    """
    text: str
    next_node: str = "exit"
    karma_change: int = 0
    coins_change: int = 0


class DialogueNodeSchema(BaseModel):
    """
    Схема для отдельного узла (реплики) диалога с NPC.
    Содержит текст реплики персонажа и список доступных вариантов ответа.
    """
    text: str
    options: List[DialogueOptionSchema] = Field(default_factory=list)


class NPCDialogueSchema(RootModel[Dict[str, DialogueNodeSchema]]):
    """
    Корневая схема диалогового древа NPC.
    Представляет собой словарь, где ключом является ID узла (например, 'start'),
    а значением — узел диалога с репликой и переходами.
    """
    pass


class StepBranchesSchema(BaseModel):
    """
    Декларативная схема сюжетных переходов (веток) на шаге.
    Содержит поле branches со словарем ответов и целевых шагов, 
    что позволяет валидировать дефолтные и пустые структуры вида {"branches": {}}.
    """
    branches: Dict[str, Union[int, str]] = Field(default_factory=dict)