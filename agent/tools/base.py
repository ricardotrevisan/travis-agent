from typing import Any, Dict


class Tool:
    """
    Classe base para ferramentas integradas à Responses API.
    """

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}

    def as_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def run(self, args: Dict[str, Any]) -> Any:
        """
        Executa a tool com os argumentos já parseados (dict).
        Deve retornar um objeto serializável em JSON.
        """
        raise NotImplementedError("Tool.run() precisa ser implementado")
