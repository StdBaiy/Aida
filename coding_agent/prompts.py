"""Compatibility facade; prompt assets and composition live in prompting."""

from coding_agent.prompting import BASE_PROMPT, assemble_prompt

SYSTEM_PROMPT = BASE_PROMPT


def build_system_prompt(skill_catalog: str = "") -> str:
    """Render the base prompt and optional bounded Skill discovery data."""
    return assemble_prompt(skill_catalog=skill_catalog).text
