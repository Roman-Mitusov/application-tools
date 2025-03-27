import logging
from typing import Dict, List, Optional
from langchain_core.tools import BaseToolkit, BaseTool
from pydantic import create_model, BaseModel, ConfigDict, Field
from functools import lru_cache

from .api_wrapper import CarrierAPIWrapper
from .tools import __all__ as available_tools
from ..utils import clean_string, TOOLKIT_SPLITTER, get_max_toolkit_length

logger = logging.getLogger(__name__)


class AlitaCarrierToolkit(BaseToolkit):
    tools: List[BaseTool] = []
    toolkit_max_length: int = 100

    @classmethod
    @lru_cache(maxsize=32)
    def toolkit_config_schema(cls) -> BaseModel:
        tool_names = {tool['name'] for tool in available_tools}
        cls.toolkit_max_length = get_max_toolkit_length({name: None for name in tool_names})

        return create_model(
            'CarrierToolkitConfig',
            url=(str, Field(description="Carrier Platform Base URL")),
            organization=(str, Field(description="Carrier Organization Name")),
            private_token=(
            str, Field(description="Carrier Platform Authentication Token", json_schema_extra={'secret': True})),
            project_id=(Optional[str], Field(None, description="Optional project ID for scoped operations")),
            selected_tools=(List[str], Field(default=list(tool_names))),
            __config__=ConfigDict(json_schema_extra={
                'metadata': {
                    "label": "Carrier Platform Toolkit",
                    "version": "2.0.1",
                    "capabilities": {
                        "total_tools": len(tool_names),
                        "tool_categories": ["Ticket Management", "Reporting", "Audit Logs"]
                    }
                }
            })
        )

    @classmethod
    def get_toolkit(
            cls,
            selected_tools: Optional[List[str]] = None,
            toolkit_name: Optional[str] = None,
            **kwargs
    ) -> 'AlitaCarrierToolkit':
        selected_tools = selected_tools or []
        logger.info(f"[AlitaCarrierToolkit] Initializing toolkit with selected tools: {selected_tools}")

        try:
            carrier_api_wrapper = CarrierAPIWrapper(**kwargs)
            logger.info(
                f"[AlitaCarrierToolkit] CarrierAPIWrapper initialized successfully with URL: {kwargs.get('url')}")
        except Exception as e:
            logger.exception(f"[AlitaCarrierToolkit] Error initializing CarrierAPIWrapper: {e}")
            raise ValueError(f"CarrierAPIWrapper initialization error: {e}")

        prefix = clean_string(toolkit_name, cls.toolkit_max_length) + TOOLKIT_SPLITTER if toolkit_name else ''

        tools = []
        for tool_def in available_tools:
            if selected_tools and tool_def['name'] not in selected_tools:
                continue
            try:
                tool_instance = tool_def['tool'](api_wrapper=carrier_api_wrapper)
                #  tool_instance.name = prefix + tool_def['name']
                tools.append(tool_instance)
                logger.info(f"[AlitaCarrierToolkit] Successfully initialized tool '{tool_instance.name}'")
            except Exception as e:
                logger.warning(f"[AlitaCarrierToolkit] Could not initialize tool '{tool_def['name']}': {e}")

        logger.info(f"[AlitaCarrierToolkit] Total tools initialized: {len(tools)}")
        return cls(tools=tools)

    def get_tools(self) -> List[BaseTool]:
        logger.info(f"[AlitaCarrierToolkit] Retrieving {len(self.tools)} initialized tools")
        return self.tools


# Simplified utility method for toolkit retrieval
def get_tools(tool_config: Dict) -> List[BaseTool]:
    return AlitaCarrierToolkit.get_toolkit(
        selected_tools=tool_config.get('selected_tools', []),
        url=tool_config['settings']['url'],
        project_id=tool_config['settings'].get('project_id'),
        organization=tool_config['settings']['organization'],
        private_token=tool_config['settings']['private_token'],
        toolkit_name=tool_config.get('toolkit_name')
    ).get_tools()
