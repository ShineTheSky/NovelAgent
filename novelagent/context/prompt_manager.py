"""Prompt模板管理"""

from pathlib import Path
from jinja2 import Environment, FileSystemLoader, TemplateNotFound


class PromptManager:
    def __init__(self, template_dir: str = "prompts"):
        self.template_dir = Path(template_dir)
        if not self.template_dir.exists():
            raise FileNotFoundError(f"Template directory not found: {template_dir}")
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=False,
        )

    def render(self, template_name: str, variables: dict | None = None) -> str:
        try:
            template = self.env.get_template(template_name)
            return template.render(**(variables or {}))
        except TemplateNotFound:
            raise FileNotFoundError(f"Template not found: {template_name}")
