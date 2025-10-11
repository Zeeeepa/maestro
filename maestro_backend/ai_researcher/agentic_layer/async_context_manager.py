import json
import uuid
from pathlib import Path
from typing import Dict, Any, Optional, List, Literal, Callable, Set 
from pydantic import BaseModel, Field, ValidationError
import datetime
import logging
import queue 
import time 
import json 
import asyncio
import re
from sqlalchemy.ext.asyncio import AsyncSession
from database import models
from database import async_crud as crud  # Use async CRUD operations
from database.async_database import get_async_db
from utils.text_sanitizer import sanitize_for_jsonb

# Use absolute imports starting from the top-level package 'ai_researcher'
from ai_researcher.config import get_current_time
from ai_researcher import config
from ai_researcher.agentic_layer.schemas.planning import SimplifiedPlan, PlanStep, ReportSection # <-- Import ReportSection
from ai_researcher.agentic_layer.schemas.research import ResearchResultResponse
from ai_researcher.agentic_layer.schemas.notes import Note # <-- Import Note schema
from ai_researcher.agentic_layer.schemas.thought import ThoughtEntry 
from ai_researcher.agentic_layer.schemas.goal import GoalEntry

# Import WebSocket update functions
from api.websockets import (
    send_plan_update, send_notes_update, send_draft_update,
    send_context_update, send_goal_pad_update, send_thought_pad_update, send_scratchpad_update,
    send_logs_update
)
from api.utils import _make_serializable

logger = logging.getLogger(__name__)


MissionStatus = Literal["planning", "running", "completed", "failed", "paused", "stopped"]

# Global reference to the main event loop for WebSocket updates
_main_event_loop = None

def set_main_event_loop():
    """Called from the main thread to store the event loop reference."""
    global _main_event_loop
    try:
        _main_event_loop = asyncio.get_running_loop()
        logger.info("Main event loop reference stored for WebSocket updates")
    except RuntimeError:
        logger.warning("No running event loop to store")

def _send_websocket_update(coroutine):
    """
    Helper function to send WebSocket updates from synchronous context.
    Uses asyncio.run_coroutine_threadsafe to properly schedule on the main event loop.
    """
    import inspect
    
    # Extract function name and mission_id for logging
    func_name = "unknown"
    mission_id = "unknown"
    if hasattr(coroutine, 'cr_code'):
        func_name = coroutine.cr_code.co_name
    if hasattr(coroutine, 'cr_frame') and coroutine.cr_frame:
        local_vars = coroutine.cr_frame.f_locals
        mission_id = local_vars.get('mission_id', 'unknown')
    
    logger.debug(f"_send_websocket_update called for {func_name} (mission: {mission_id})")
    
    try:
        # Try to get the running loop
        loop = asyncio.get_running_loop()
        # We're already in an async context, just create a task
        task = asyncio.create_task(coroutine)
        # Add error handler to log any exceptions
        def handle_exception(task):
            if not task.cancelled():
                exc = task.exception()
                if exc:
                    logger.error(f"WebSocket update failed for {func_name}: {exc}")
                else:
                    logger.debug(f"WebSocket update completed for {func_name} (mission: {mission_id})")
        task.add_done_callback(handle_exception)
        logger.debug(f"WebSocket update task created in async context for {func_name}")
    except RuntimeError:
        # No running loop in current thread - we're in a sync context (background thread)
        # Need to use the main event loop to send WebSocket updates
        global _main_event_loop
        
        logger.debug(f"In sync context, attempting to send {func_name} update to main loop")
        
        try:
            # First try to use the stored main loop
            if _main_event_loop and _main_event_loop.is_running():
                # Use run_coroutine_threadsafe to schedule on the main loop
                future = asyncio.run_coroutine_threadsafe(coroutine, _main_event_loop)
                
                # Add callback to log completion
                def log_result(fut):
                    try:
                        result = fut.result(timeout=0.1)  # Quick check, don't block
                        logger.debug(f"WebSocket update {func_name} completed successfully (mission: {mission_id})")
                    except asyncio.TimeoutError:
                        logger.debug(f"WebSocket update {func_name} scheduled but not yet complete (mission: {mission_id})")
                    except Exception as e:
                        logger.error(f"WebSocket update {func_name} failed: {e}")
                
                future.add_done_callback(log_result)
                logger.info(f"WebSocket update {func_name} scheduled on main event loop (mission: {mission_id})")
            else:
                logger.warning(f"Main event loop not available for {func_name}, falling back to thread")
                # Fallback: run in a separate thread with its own loop
                import threading
                def run_in_thread():
                    try:
                        asyncio.run(coroutine)
                        logger.info(f"WebSocket update {func_name} sent via new thread (mission: {mission_id})")
                    except Exception as thread_error:
                        logger.error(f"WebSocket update {func_name} failed in thread: {thread_error}")
                
                thread = threading.Thread(target=run_in_thread, daemon=True)
                thread.start()
        except Exception as e:
            logger.error(f"Failed to send WebSocket update {func_name}: {e}")

# --- New Schema for Execution Log ---
class ExecutionLogEntry(BaseModel):
    """Represents a single step in the mission execution log."""
    log_id: str = Field(default_factory=lambda: str(uuid.uuid4()))  # Unique ID for each log entry
    timestamp: datetime.datetime = Field(default_factory=get_current_time)
    agent_name: str
    action: str # e.g., "Generating Plan", "Running Research", "Writing Section"
    input_summary: Optional[str] = None # Brief description of input
    output_summary: Optional[str] = None # Brief description of output/result
    status: Literal["success", "failure", "warning", "running"] = "success" # Added "running"
    error_message: Optional[str] = None
    # --- Added fields for detailed logging ---
    full_input: Optional[Any] = Field(None, description="Detailed input data (e.g., dict, list, long text)")
    full_output: Optional[Any] = Field(None, description="Detailed output data")
    model_details: Optional[Dict[str, Any]] = Field(None, description="Details about the LLM call (model, provider, duration, etc.)")
    tool_calls: Optional[List[Dict[str, Any]]] = Field(None, description="Details about tool calls made during the step")
    file_interactions: Optional[List[str]] = Field(None, description="Record of files read/written during the step")
    # --- End added fields ---

# --- Updated Mission Context ---
class MissionContext(BaseModel):
    """Holds the state for a single research mission."""
    mission_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_request: str
    status: MissionStatus = "planning"
    plan: Optional[SimplifiedPlan] = None
    step_results: Dict[str, ResearchResultResponse] = Field(default_factory=dict)
    notes: List[Note] = Field(default_factory=list, description="List of notes gathered during research.")
    report_content: Dict[str, str] = Field(default_factory=dict)
    final_report: Optional[str] = None
    message_history: List[Dict[str, str]] = Field(default_factory=list)
    created_at: datetime.datetime = Field(default_factory=get_current_time)
    updated_at: datetime.datetime = Field(default_factory=get_current_time)
    error_info: Optional[str] = None # Store error details if mission fails
    agent_scratchpad: Optional[str] = Field(None, description="Dynamic scratchpad for high-level agent context and insights.") # <-- Add scratchpad
    execution_log: List[ExecutionLogEntry] = Field(default_factory=list, description="Log of agent actions and results.")
    writing_suggestions: Optional[List[Any]] = Field(default=None, description="Writing revision suggestions from reflection passes.")
    goal_pad: List[GoalEntry] = Field(default_factory=list, description="Persistent list of research goals and guiding thoughts.") # <-- ADDED goal_pad
    thought_pad: List[ThoughtEntry] = Field(default_factory=list, description="Working memory holding recent thoughts and focus points.") # <-- ADDED thought_pad
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata for the mission (e.g., questions, refinements).")
    
    # Comprehensive mission settings
    mission_settings: Optional[Dict[str, Any]] = Field(None, description="All settings used for this mission")
    document_group_id: Optional[str] = Field(None, description="Document group ID if provided")
    document_group_name: Optional[str] = Field(None, description="Document group name if provided")
    use_web_search: bool = Field(True, description="Whether web search was enabled")
    
    # Model configurations used
    llm_config: Optional[Dict[str, Any]] = Field(None, description="LLM models and providers used")
    
    # Research parameters snapshot
    research_params: Optional[Dict[str, Any]] = Field(None, description="Research parameters used")
    
    # Cost and statistics
    total_cost: float = Field(0.0, description="Total cost of the mission")
    total_tokens: Dict[str, int] = Field(default_factory=lambda: {"prompt": 0, "completion": 0, "native": 0})
    total_web_searches: int = Field(0, description="Total web searches performed")
    
    # Resumable execution tracking
    execution_phase: str = Field(default="not_started", description="Current execution phase")
    completed_phases: List[str] = Field(default_factory=list, description="List of completed phases")
    phase_checkpoint: Dict[str, Any] = Field(default_factory=dict, description="Checkpoint data for resuming phases")
    
    # Current phase tracking for UI display
    current_phase_display: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Current phase info for UI display")
    
    # Reference ID mapping for simplified citations
    reference_id_map: Dict[str, str] = Field(default_factory=dict, description="Maps UUID/complex IDs to simple reference IDs (e.g., 'ref1', 'ref2')")
    reverse_reference_map: Dict[str, str] = Field(default_factory=dict, description="Maps simple reference IDs back to original UUIDs")
    reference_counter: int = Field(default=0, description="Counter for generating sequential reference IDs")

    def update_timestamp(self):
        self.updated_at = get_current_time()
    
    def get_simple_reference_id(self, original_id: str) -> str:
        """Get or create a simple reference ID for a complex UUID."""
        if original_id not in self.reference_id_map:
            self.reference_counter += 1
            simple_id = f"ref{self.reference_counter}"
            self.reference_id_map[original_id] = simple_id
            self.reverse_reference_map[simple_id] = original_id
        return self.reference_id_map[original_id]
    
    def get_original_reference_id(self, simple_id: str) -> Optional[str]:
        """Get the original UUID from a simple reference ID."""
        return self.reverse_reference_map.get(simple_id)


def sanitize_for_jsonb(obj: Any) -> Any:
    """
    Recursively sanitize an object to remove null characters and other 
    problematic Unicode characters that PostgreSQL JSONB cannot handle.
    """
    if isinstance(obj, str):
        # Remove null characters and other control characters
        # Keep tabs, newlines, and carriage returns (0x09, 0x0A, 0x0D)
        # Remove other control characters (0x00-0x08, 0x0B, 0x0C, 0x0E-0x1F)
        cleaned = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F]', '', obj)
        return cleaned
    elif isinstance(obj, dict):
        return {k: sanitize_for_jsonb(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_for_jsonb(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(sanitize_for_jsonb(item) for item in obj)
    else:
        # Return other types as-is (numbers, booleans, None, etc.)
        return obj


class AsyncContextManager:
    """
    Async version of ContextManager.
    Manages the state and history for multiple research missions.
    Stores context in memory and persists it to the database asynchronously.
    """
    def __init__(self):
        self._missions: Dict[str, MissionContext] = {}
        # --- NEW: State for Tracking LLM Usage (Moved from AgentController) ---
        # Stores cumulative stats per mission
        self.mission_stats: Dict[str, Dict[str, float]] = {} # mission_id -> {"total_cost": float, "total_prompt_tokens": float, "total_completion_tokens": float, "total_native_tokens": float, "total_web_search_calls": int}
        # Per-mission semaphores for concurrency control
        self._mission_semaphores: Dict[str, asyncio.Semaphore] = {}
        self.tracked_calls: Set[str] = set() # Track call IDs to prevent double counting
        # --- End NEW State ---
        
        logger.info("AsyncContextManager initialized. Call async_init() to load missions from database.")

    async def async_init(self):
        """Async initialization - load all missions from database."""
        await self._load_all_missions_from_db()
    
    async def _load_all_missions_from_db(self):
        """Loads all existing missions from the database into the in-memory cache on startup."""
        async with get_async_db() as db:
            all_db_missions = await crud.get_all_missions(db)
            loaded_count = 0
            for db_mission in all_db_missions:
                try:
                    # The mission_context from DB is a dict, convert it back to Pydantic model
                    if db_mission.mission_context:
                        # Migrate notes to add missing timestamp fields before validation
                        migrated_context = self._migrate_mission_context(db_mission.mission_context)
                        mission_context_model = MissionContext(**migrated_context)
                        self._missions[db_mission.id] = mission_context_model
                        loaded_count += 1
                    else:
                        # Handle cases where a mission might exist in DB but with no context
                        # This could be a fallback or recovery mechanism
                        logger.warning(f"Mission '{db_mission.id}' found in DB but has no context. Creating a default.")
                        mission_context_model = MissionContext(
                            mission_id=db_mission.id,
                            user_request=db_mission.user_request,
                            status=db_mission.status,
                            created_at=db_mission.created_at,
                            updated_at=db_mission.updated_at,
                            error_info=db_mission.error_info
                        )
                        self._missions[db_mission.id] = mission_context_model
                        loaded_count += 1

                except ValidationError as e:
                    logger.error(f"Pydantic validation error loading mission '{db_mission.id}' from DB: {e}", exc_info=True)
                except Exception as e:
                    logger.error(f"Unexpected error loading mission '{db_mission.id}' from DB: {e}", exc_info=True)
            
            logger.info(f"Successfully loaded {loaded_count} missions from the database into memory.")

    def _migrate_mission_context(self, context_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Migrates mission context data to ensure compatibility with current schema.
        Adds missing timestamp fields to notes and handles other schema changes.
        """
        migrated_context = context_dict.copy()

        # Migrate notes to add missing timestamp fields
        if 'notes' in migrated_context and isinstance(migrated_context['notes'], list):
            current_time = get_current_time()
            migrated_notes = []

            for note_data in migrated_context['notes']:
                if isinstance(note_data, dict):
                    # Add missing timestamp fields if they don't exist
                    if 'created_at' not in note_data:
                        note_data['created_at'] = current_time
                    if 'updated_at' not in note_data:
                        note_data['updated_at'] = current_time
                    migrated_notes.append(note_data)
                else:
                    # Skip invalid note data
                    logger.warning(f"Skipping invalid note data during migration: {note_data}")

            migrated_context['notes'] = migrated_notes
            # logger.info(f"Migrated {len(migrated_notes)} notes with timestamp fields")

        # Ensure metadata has source configuration fields
        if 'metadata' in migrated_context and migrated_context['metadata']:
            metadata = migrated_context['metadata']

            # Check if we have comprehensive_settings to restore from
            if 'comprehensive_settings' in metadata:
                comp_settings = metadata['comprehensive_settings']
                # Ensure top-level metadata has the source settings for easy access
                if 'use_web_search' not in metadata and 'use_web_search' in comp_settings:
                    metadata['use_web_search'] = comp_settings['use_web_search']
                if 'use_local_rag' not in metadata and 'use_local_rag' in comp_settings:
                    metadata['use_local_rag'] = comp_settings['use_local_rag']
                if 'document_group_id' not in metadata and 'document_group_id' in comp_settings:
                    metadata['document_group_id'] = comp_settings['document_group_id']
                if 'document_group_name' not in metadata and 'document_group_name' in comp_settings:
                    metadata['document_group_name'] = comp_settings['document_group_name']

            # Also check mission_settings field for source configuration
            if 'mission_settings' in metadata and metadata['mission_settings']:
                mission_settings = metadata['mission_settings']
                if 'use_web_search' not in metadata and 'search_provider' in mission_settings:
                    metadata['use_web_search'] = True  # If there's a search provider, web search was enabled

            # Ensure tool_selection exists if we have the data
            if 'tool_selection' not in metadata:
                metadata['tool_selection'] = {
                    'web_search': metadata.get('use_web_search', migrated_context.get('use_web_search', False)),
                    'local_rag': metadata.get('use_local_rag', bool(metadata.get('document_group_id')))
                }

        # Also ensure top-level use_web_search field if missing
        if 'use_web_search' not in migrated_context:
            # Try to get from metadata
            if 'metadata' in migrated_context and migrated_context['metadata']:
                migrated_context['use_web_search'] = migrated_context['metadata'].get('use_web_search', True)
            else:
                migrated_context['use_web_search'] = True  # Default to true for backward compatibility

        return migrated_context

    # --- Public Methods ---
    
    def get_mission_semaphore(self, mission_id: str, max_concurrent: Optional[int] = None) -> asyncio.Semaphore:
        """Get or create a semaphore for a specific mission.
        
        Args:
            mission_id: The mission ID
            max_concurrent: Maximum concurrent operations for this mission.
                          If None, uses user's max_concurrent_requests setting divided by 2
                          to allow multiple missions to run concurrently.
        """
        if mission_id not in self._mission_semaphores:
            if max_concurrent is None:
                # Get user's setting for this mission
                from ai_researcher.dynamic_config import get_max_concurrent_requests
                user_max = get_max_concurrent_requests(mission_id)
                # Use half for per-mission to allow multiple missions, minimum 3
                max_concurrent = max(3, user_max // 2) if user_max > 0 else 10
            
            self._mission_semaphores[mission_id] = asyncio.Semaphore(max_concurrent)
            logger.info(f"Created semaphore for mission {mission_id} with max_concurrent={max_concurrent}")
        return self._mission_semaphores[mission_id]

    async def start_mission(self, user_request: str, chat_id: str, 
                      document_group_id: Optional[str] = None,
                      document_group_name: Optional[str] = None,
                      use_web_search: bool = True,
                      mission_settings: Optional[Dict[str, Any]] = None,
                      llm_config: Optional[Dict[str, Any]] = None,
                      research_params: Optional[Dict[str, Any]] = None) -> MissionContext:
        """Creates and stores context for a new mission with comprehensive settings."""
        """Creates a new mission, stores it in the database, and adds it to the in-memory cache."""
        mission = MissionContext(
            user_request=user_request,
            document_group_id=document_group_id,
            document_group_name=document_group_name,
            use_web_search=use_web_search,
            mission_settings=mission_settings,
            llm_config=llm_config,
            research_params=research_params
        )
        mission.metadata["chat_id"] = chat_id

        self._missions[mission.mission_id] = mission
        
        async with get_async_db() as db:
            try:
                sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                await crud.create_mission(
                    db=db,
                    mission_id=mission.mission_id,
                    chat_id=chat_id,
                    user_request=user_request,
                    mission_context=sanitized_context
                )
                logger.info(f"Started and saved new mission: {mission.mission_id} for chat: {chat_id}")
            except Exception as e:
                logger.error(f"Database error creating mission {mission.mission_id}: {e}", exc_info=True)
                # If DB write fails, remove from in-memory cache to avoid inconsistent state
                del self._missions[mission.mission_id]
                raise  # Re-raise the exception to be handled by the caller
            
        return mission

    def get_mission_context(self, mission_id: str) -> Optional[MissionContext]:
        """
        Retrieves the context for a given mission ID primarily from the in-memory store.
        Loading from disk should happen explicitly, e.g., during initialization if needed.
        """
        """Retrieves the context for a given mission ID from the in-memory cache."""
        mission = self._missions.get(mission_id)
        if not mission:
            logger.warning(f"Mission context not found in memory for ID: {mission_id}. Returning None.")
        return mission
    
    def remove_mission_from_memory(self, mission_id: str) -> bool:
        """
        Remove a mission from in-memory storage.
        This is used when a mission is deleted or needs to be force-stopped.
        Returns True if mission was removed, False if not found.
        """
        if mission_id in self._missions:
            # First mark as stopped to prevent further operations
            mission = self._missions[mission_id]
            mission.status = "stopped"
            
            # Remove from memory
            del self._missions[mission_id]
            
            # Clean up semaphore if exists
            if mission_id in self._mission_semaphores:
                del self._mission_semaphores[mission_id]
            
            # Cancel any async tasks
            from ai_researcher.agentic_layer.controller.utils.async_task_manager import get_task_manager
            task_manager = get_task_manager()
            asyncio.create_task(task_manager.cancel_mission_tasks(mission_id))
            
            # Stop the mission thread/loop
            from ai_researcher.agentic_layer.controller.utils.mission_lifecycle import get_lifecycle_manager
            lifecycle_manager = get_lifecycle_manager()
            lifecycle_manager.stop_mission(mission_id)
            lifecycle_manager.cleanup_mission(mission_id)
            
            logger.info(f"Removed mission {mission_id} from memory and stopped all operations")
            return True
        return False

    async def update_mission_status(self, mission_id: str, status: MissionStatus, error_info: Optional[str] = None):
        """Updates the status of a mission in memory and in the database, and sends WebSocket update."""
        mission = self.get_mission_context(mission_id)
        if mission:
            old_status = mission.status
            mission.status = status
            logger.info(f"[STATUS UPDATE] Mission {mission_id} status changed: {old_status} -> {status}")
            mission.error_info = error_info if status == "failed" else None
            mission.update_timestamp()
            
            # Clean up semaphore for completed/failed missions
            if status in ["completed", "failed"] and mission_id in self._mission_semaphores:
                del self._mission_semaphores[mission_id]
                logger.info(f"Cleaned up semaphore for {status} mission {mission_id}")
            
            async with get_async_db() as db:
                try:
                    await crud.update_mission_status(db, mission_id=mission_id, status=status, error_info=error_info)
                    sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                    await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                    logger.info(f"Updated mission '{mission_id}' status to '{status}' in DB.")
                    
                    # Send WebSocket update to frontend
                    try:
                        from api.websockets import send_status_update
                        await send_status_update(
                            mission_id=mission_id,
                            status=status,
                            metadata={
                                "error_info": error_info,
                                "timestamp": mission.updated_at.isoformat() if mission.updated_at else None
                            }
                        )
                        logger.info(f"Sent WebSocket status update for mission '{mission_id}' to '{status}'")
                    except Exception as ws_error:
                        logger.error(f"Failed to send WebSocket update for mission {mission_id}: {ws_error}")
                        
                except Exception as e:
                    logger.error(f"Database error updating mission status for {mission_id}: {e}", exc_info=True)
        else:
            logger.error(f"Cannot update status for non-existent mission ID: {mission_id}")
    
    async def update_execution_phase(self, mission_id: str, phase: str, checkpoint_data: Optional[Dict[str, Any]] = None):
        """Updates the current execution phase and optionally saves checkpoint data."""
        mission = self.get_mission_context(mission_id)
        if mission:
            mission.execution_phase = phase
            if checkpoint_data:
                mission.phase_checkpoint.update(checkpoint_data)
            mission.update_timestamp()
            
            # Save to database
            async with get_async_db() as db:
                try:
                    sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                    await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                    logger.info(f"Updated mission {mission_id} to phase: {phase}")
                except Exception as e:
                    logger.error(f"Failed to update execution phase in database: {e}")
    
    async def mark_phase_completed(self, mission_id: str, phase: str):
        """Marks a phase as completed."""
        mission = self.get_mission_context(mission_id)
        if mission:
            if phase not in mission.completed_phases:
                mission.completed_phases.append(phase)
                mission.update_timestamp()
                
                # Save to database
                async with get_async_db() as db:
                    try:
                        sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                        await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                        logger.info(f"Marked phase '{phase}' as completed for mission {mission_id}")
                    except Exception as e:
                        logger.error(f"Failed to mark phase as completed in database: {e}")
    
    def get_next_phase(self, mission_id: str) -> Optional[str]:
        """Returns the next phase to execute based on completed phases and current phase."""
        mission = self.get_mission_context(mission_id)
        if not mission:
            return None
        
        # Define the phase order
        phase_order = [
            "initial_analysis",
            "initial_research", 
            "outline_generation",
            "structured_research",
            "note_preparation",
            "writing",
            "title_generation",
            "citation_processing",
            "completed"
        ]
        
        # First check if there's a current phase that was interrupted
        # Check phase_checkpoint for any in-progress phases
        for phase_name in phase_order:
            if phase_name in mission.phase_checkpoint and phase_name not in mission.completed_phases:
                phase_data = mission.phase_checkpoint[phase_name]
                # If there's checkpoint data, this phase was started but not completed
                if phase_data:
                    logger.info(f"Mission {mission_id} has in-progress phase: {phase_name}")
                    return phase_name
        
        # Also check the resume checkpoint for phase info
        checkpoint = self.get_resume_checkpoint(mission_id)
        if checkpoint and checkpoint.get('phase'):
            checkpoint_phase = checkpoint['phase']
            if checkpoint_phase not in mission.completed_phases:
                logger.info(f"Mission {mission_id} has checkpoint for phase: {checkpoint_phase}")
                return checkpoint_phase
        
        # Find the next uncompleted phase
        for phase in phase_order:
            if phase not in mission.completed_phases:
                return phase
        
        return "completed"
    
    # DEPRECATED: Use save_phase_checkpoint instead for consistency
    # def store_resume_checkpoint(self, mission_id: str, checkpoint: Dict[str, Any]):
    #     """Store checkpoint information for resuming a mission."""
    #     # This method has been replaced with save_phase_checkpoint for consistency
    
    def get_resume_checkpoint(self, mission_id: str) -> Dict[str, Any]:
        """Get detailed checkpoint information for resuming a mission."""
        mission = self.get_mission_context(mission_id)
        if not mission:
            return {}
        
        checkpoint = {
            "current_phase": mission.execution_phase,
            "completed_phases": mission.completed_phases,
            "phase_checkpoint": mission.phase_checkpoint,
            "has_plan": mission.plan is not None,
            "notes_count": len(mission.notes),
            "sections_written": list(mission.report_content.keys()) if mission.report_content else [],
            "last_activity": None
        }
        
        # Analyze execution log to find last activity
        if mission.execution_log:
            last_log = mission.execution_log[-1]
            checkpoint["last_activity"] = {
                "agent": last_log.agent_name,
                "action": last_log.action,
                "timestamp": last_log.timestamp,
                "status": last_log.status
            }
        
        # For structured research phase, track which sections were completed
        if mission.execution_phase == "structured_research" and mission.plan:
            completed_sections = mission.phase_checkpoint.get("completed_sections", [])
            sections_in_progress = mission.phase_checkpoint.get("sections_in_progress", {})
            checkpoint["structured_research_progress"] = {
                "completed_sections": completed_sections,
                "sections_in_progress": sections_in_progress,
                "total_sections": len(mission.plan.research_sections) if mission.plan.research_sections else 0
            }
        
        return checkpoint
    
    async def save_phase_checkpoint(self, mission_id: str, phase: str, checkpoint_data: Dict[str, Any]):
        """Save checkpoint data for a specific phase to enable granular resume."""
        mission = self.get_mission_context(mission_id)
        if mission:
            if phase not in mission.phase_checkpoint:
                mission.phase_checkpoint[phase] = {}
            mission.phase_checkpoint[phase].update(checkpoint_data)
            mission.update_timestamp()
            
            async with get_async_db() as db:
                try:
                    sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                    await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                    logger.debug(f"Saved checkpoint for phase '{phase}' in mission {mission_id}")
                except Exception as e:
                    logger.error(f"Failed to save phase checkpoint: {e}", exc_info=True)
    
    async def get_phase_checkpoint(self, mission_id: str, phase: str) -> Optional[Dict[str, Any]]:
        """Get checkpoint data for a specific phase."""
        mission = self.get_mission_context(mission_id)
        if mission:
            return mission.phase_checkpoint.get(phase)
        return None

    async def store_plan(self, mission_id: str, plan: SimplifiedPlan):
        """Stores the generated plan for a mission in memory and persists the context to the database."""
        mission = self.get_mission_context(mission_id)
        if mission:
            mission.plan = plan
            mission.status = "running"  # Typically moves to running after planning
            mission.update_timestamp()
            
            async with get_async_db() as db:
                try:
                    # Persist the entire updated context
                    sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                    await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                    # Also update the status explicitly in the main mission table
                    await crud.update_mission_status(db, mission_id=mission_id, status="running")
                    logger.info(f"Stored plan and updated context for mission '{mission_id}' in DB.")
                    
                    # Send WebSocket update for plan
                    try:
                        plan_dict = plan.model_dump() if hasattr(plan, 'model_dump') else plan
                        _send_websocket_update(send_plan_update(mission_id, plan_dict, "update"))
                        logger.info(f"Sent plan update via WebSocket for mission '{mission_id}'.")
                    except Exception as ws_error:
                        logger.error(f"Failed to send plan update via WebSocket for mission {mission_id}: {ws_error}")
                except Exception as e:
                    logger.error(f"Database error storing plan for mission {mission_id}: {e}", exc_info=True)
        else:
            logger.error(f"Cannot store plan for non-existent mission ID: {mission_id}")

    async def store_step_result(self, mission_id: str, result: ResearchResultResponse):
        """Stores the result of a plan step and persists the updated context to the database."""
        mission = self.get_mission_context(mission_id)
        if mission:
            step_id = result.step_id
            mission.step_results[step_id] = result
            mission.update_timestamp()
            
            async with get_async_db() as db:
                try:
                    sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                    await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                    logger.info(f"Stored result for step '{step_id}' in mission '{mission_id}' and updated DB.")
                except Exception as e:
                    logger.error(f"Database error storing step result for mission {mission_id}: {e}", exc_info=True)
        else:
            logger.error(f"Cannot store step result for non-existent mission ID: {mission_id}")

    def get_step_results(self, mission_id: str, step_ids: Optional[List[str]] = None) -> Dict[str, ResearchResultResponse]:
        """Retrieves results for specific steps or all steps if step_ids is None."""
        mission = self.get_mission_context(mission_id)
        if not mission:
            return {}
        if step_ids:
            return {sid: res for sid, res in mission.step_results.items() if sid in step_ids}
        else:
            return mission.step_results # Return all results

    async def store_report_section(self, mission_id: str, section_id: str, content: str):
        """Stores report section content and persists the updated context to the database."""
        mission = self.get_mission_context(mission_id)
        if mission:
            mission.report_content[section_id] = content
            mission.update_timestamp()
            
            async with get_async_db() as db:
                try:
                    sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                    await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                    logger.info(f"Stored report section '{section_id}' for mission '{mission_id}' and updated DB.")
                    
                    # Send WebSocket update for draft
                    try:
                        current_draft = self.build_draft_from_context(mission_id)
                        if current_draft:
                            _send_websocket_update(send_draft_update(mission_id, current_draft, "update"))
                            logger.info(f"Sent draft update via WebSocket for mission '{mission_id}'.")
                    except Exception as ws_error:
                        logger.error(f"Failed to send draft update via WebSocket for mission {mission_id}: {ws_error}")
                except Exception as e:
                    logger.error(f"Database error storing report section for mission {mission_id}: {e}", exc_info=True)
        else:
            logger.error(f"Cannot store report section for non-existent mission ID: {mission_id}")

    async def store_final_report(self, mission_id: str, report_text: str, revision_notes: Optional[str] = None):
        """Stores the final report, updates status, and persists the context to the database.
        Creates a new version in the research_reports table if this is a revision."""
        mission = self.get_mission_context(mission_id)
        if mission:
            mission.final_report = report_text
            mission.status = "completed"  # Mark as completed
            mission.update_timestamp()
            
            async with get_async_db() as db:
                try:
                    # Update mission status and context
                    await crud.update_mission_status(db, mission_id=mission_id, status="completed")
                    sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                    await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                    
                    # Create a versioned research report
                    # Use synchronous database session for the CRUD operation
                    from database.database import SessionLocal
                    from database.crud_research_reports import create_research_report
                    
                    # Extract title from report if available
                    title = None
                    if report_text:
                        lines = report_text.split('\n')
                        for line in lines:
                            if line.strip().startswith('# '):
                                title = line.strip()[2:].strip()
                                break
                    
                    # Create the versioned report using a synchronous session
                    try:
                        sync_db = SessionLocal()
                        try:
                            create_research_report(
                                sync_db,
                                mission_id,
                                report_text,
                                title,
                                revision_notes,
                                True  # make_current
                            )
                            sync_db.commit()
                            logger.info(f"Created research report version for mission {mission_id}")
                        except Exception as e:
                            sync_db.rollback()
                            logger.error(f"Failed to create research report version: {e}")
                            raise
                        finally:
                            sync_db.close()
                    except Exception as e:
                        logger.error(f"Error creating research report: {e}", exc_info=True)
                    
                    logger.info(f"Stored final report and set status to 'completed' for mission '{mission_id}' in DB.")
                    
                    # Send WebSocket update for final report
                    try:
                        _send_websocket_update(send_draft_update(mission_id, report_text, "report"))
                        logger.info(f"Sent final report update via WebSocket for mission '{mission_id}'.")
                    except Exception as ws_error:
                        logger.error(f"Failed to send final report update via WebSocket for mission {mission_id}: {ws_error}")
                except Exception as e:
                    logger.error(f"Database error storing final report for mission {mission_id}: {e}", exc_info=True)
        else:
            logger.error(f"Cannot store final report for non-existent mission ID: {mission_id}")

    # Add methods for message history if needed
    def add_message_to_history(self, mission_id: str, message: Dict[str, str]):
         mission = self.get_mission_context(mission_id)
         if mission:
              mission.message_history.append(message)
              mission.update_timestamp()
              # Don't save after every message for performance, maybe save periodically or on demand
              # self._save_mission(mission_id)

    # --- Note Management Methods ---

    async def update_writing_suggestions(self, mission_id: str, suggestions: List[Any]):
        """Updates writing suggestions for a mission and persists to database."""
        mission = self.get_mission_context(mission_id)
        if mission:
            mission.writing_suggestions = suggestions
            mission.update_timestamp()
            
            async with get_async_db() as db:
                try:
                    sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                    await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                    logger.debug(f"Updated writing suggestions for mission {mission_id} with {len(suggestions)} suggestions.")
                except Exception as e:
                    logger.error(f"Error updating writing suggestions in DB: {e}")
        else:
            logger.error(f"Cannot update writing suggestions for non-existent mission ID: {mission_id}")

    def get_writing_suggestions(self, mission_id: str) -> List[Any]:
        """Retrieves writing suggestions for a mission."""
        mission = self.get_mission_context(mission_id)
        if mission:
            # Handle missions created before writing_suggestions field was added
            if hasattr(mission, 'writing_suggestions') and mission.writing_suggestions is not None:
                return mission.writing_suggestions
            else:
                return []
        return []

    async def add_note(self, mission_id: str, note: Note):
        """Adds a single note and persists the updated context to the database."""
        mission = self.get_mission_context(mission_id)
        if mission:
            mission.notes.append(note)
            mission.update_timestamp()
            
            # Process note for auto-created document group if enabled
            await self._process_note_for_document_group(mission_id, note)
            
            async with get_async_db() as db:
                try:
                    sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                    await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                    logger.debug(f"Added note {note.note_id} to mission {mission_id} and updated DB.")
                except Exception as e:
                    logger.error(f"Database error adding note for mission {mission_id}: {e}", exc_info=True)
            
            # Send WebSocket update for note
            try:
                # Import and use the transformation function for consistency
                from api.missions import transform_note_for_frontend
                import asyncio
                
                # Check if we're in an async context or not
                try:
                    loop = asyncio.get_running_loop()
                    # We're in an async context, schedule the coroutine
                    async def send_note_update():
                        note_dict = await transform_note_for_frontend(note)
                        await send_notes_update(mission_id, [note_dict], "append")
                    asyncio.ensure_future(send_note_update())
                    logger.info(f"Scheduled note update via WebSocket for mission '{mission_id}' (1 note).")
                except RuntimeError:
                    # No running loop, we're in a sync context
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        note_dict = loop.run_until_complete(transform_note_for_frontend(note))
                    finally:
                        loop.close()
                    _send_websocket_update(send_notes_update(mission_id, [note_dict], "append"))
                    logger.info(f"Sent note update via WebSocket for mission '{mission_id}' (1 note).")
            except Exception as ws_error:
                logger.error(f"Failed to send note update via WebSocket for mission {mission_id}: {ws_error}")
        else:
            logger.error(f"Cannot add note for non-existent mission ID: {mission_id}")

    async def add_notes(self, mission_id: str, notes: List[Note]):
        """Adds a list of notes and persists the updated context to the database."""
        mission = self.get_mission_context(mission_id)
        if mission:
            mission.notes.extend(notes)
            mission.update_timestamp()
            
            # Process notes for auto-created document group if enabled
            for note in notes:
                # Log note details for debugging
                if hasattr(note, 'source_type') and note.source_type == 'web':
                    metadata = note.source_metadata if hasattr(note, 'source_metadata') else None
                    if metadata:
                        if isinstance(metadata, dict):
                            has_full = metadata.get('fetched_full_content', False)
                            logger.info(f"Processing web note {note.note_id}: fetched_full_content={has_full}, source={note.source_id if hasattr(note, 'source_id') else 'unknown'}")
                        else:
                            has_full = getattr(metadata, 'fetched_full_content', False)
                            logger.info(f"Processing web note {note.note_id}: fetched_full_content={has_full}, source={note.source_id if hasattr(note, 'source_id') else 'unknown'}")
                await self._process_note_for_document_group(mission_id, note)
            
            async with get_async_db() as db:
                try:
                    sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                    await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                    logger.info(f"Added {len(notes)} notes to mission {mission_id} and updated DB.")
                except Exception as e:
                    logger.error(f"Database error adding notes for mission {mission_id}: {e}", exc_info=True)
            
            # Send WebSocket update for notes
            try:
                # Import and use the transformation function for consistency
                from api.missions import transform_note_for_frontend
                import asyncio
                
                # Check if we're in an async context or not
                try:
                    loop = asyncio.get_running_loop()
                    # We're in an async context, schedule the coroutine
                    async def send_notes_update_async():
                        tasks = [transform_note_for_frontend(note) for note in notes]
                        notes_list = await asyncio.gather(*tasks)
                        await send_notes_update(mission_id, notes_list, "append")
                    asyncio.ensure_future(send_notes_update_async())
                    logger.info(f"Scheduled notes update via WebSocket for mission '{mission_id}' ({len(notes)} notes).")
                except RuntimeError:
                    # No running loop, we're in a sync context
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        # Transform all notes asynchronously
                        async def transform_all_notes():
                            tasks = [transform_note_for_frontend(note) for note in notes]
                            return await asyncio.gather(*tasks)
                        notes_list = loop.run_until_complete(transform_all_notes())
                    finally:
                        loop.close()
                    _send_websocket_update(send_notes_update(mission_id, notes_list, "append"))
                    logger.info(f"Sent notes update via WebSocket for mission '{mission_id}' ({len(notes)} notes).")
            except Exception as ws_error:
                logger.error(f"Failed to send notes update via WebSocket for mission {mission_id}: {ws_error}")
        else:
            logger.error(f"Cannot add notes for non-existent mission ID: {mission_id}")

    def get_notes(self, mission_id: str) -> List[Note]:
        """Retrieves all notes for a given mission ID."""
        mission = self.get_mission_context(mission_id)
        if mission:
            return mission.notes
        else:
            logger.warning(f"Cannot get notes for non-existent mission ID: {mission_id}")
            return []

    async def remove_notes(self, mission_id: str, note_ids_to_remove: List[str]):
        """Removes notes and persists the updated context to the database."""
        mission = self.get_mission_context(mission_id)
        if mission:
            initial_count = len(mission.notes)
            ids_to_remove_set = set(note_ids_to_remove)
            mission.notes = [note for note in mission.notes if note.note_id not in ids_to_remove_set]
            final_count = len(mission.notes)
            removed_count = initial_count - final_count
            
            if removed_count > 0:
                mission.update_timestamp()
                async with get_async_db() as db:
                    try:
                        sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                        await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                        logger.info(f"Removed {removed_count} notes from mission {mission_id} and updated DB.")
                    except Exception as e:
                        logger.error(f"Database error removing notes for mission {mission_id}: {e}", exc_info=True)
            else:
                logger.warning(f"Attempted to remove notes, but none of the specified IDs were found in mission {mission_id}. IDs: {note_ids_to_remove}")
        else:
            logger.error(f"Cannot remove notes for non-existent mission ID: {mission_id}")

    # --- Scratchpad Management Methods ---

    async def update_scratchpad(self, mission_id: str, scratchpad_content: Optional[str]):
        """Updates the agent scratchpad and persists the updated context to the database."""
        mission = self.get_mission_context(mission_id)
        if mission:
            if mission.agent_scratchpad != scratchpad_content:  # Only update if changed
                mission.agent_scratchpad = scratchpad_content
                mission.update_timestamp()
                
                async with get_async_db() as db:
                    try:
                        sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                        await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                        logger.debug(f"Updated scratchpad for mission {mission_id} and updated DB.")
                        
                        # Send WebSocket update for scratchpad
                        try:
                            _send_websocket_update(send_scratchpad_update(mission_id, scratchpad_content or "", "update"))
                            logger.info(f"Sent scratchpad update via WebSocket for mission '{mission_id}'.")
                        except Exception as ws_error:
                            logger.error(f"Failed to send scratchpad update via WebSocket for mission {mission_id}: {ws_error}")
                    except Exception as e:
                        logger.error(f"Database error updating scratchpad for mission {mission_id}: {e}", exc_info=True)
        else:
            logger.error(f"Cannot update scratchpad for non-existent mission ID: {mission_id}")

    def get_scratchpad(self, mission_id: str) -> Optional[str]:
        """Retrieves the current agent scratchpad content for a mission."""
        mission = self.get_mission_context(mission_id)
        if mission:
            return mission.agent_scratchpad
        else:
            logger.warning(f"Cannot get scratchpad for non-existent mission ID: {mission_id}")
            return None
    
    def get_plan(self, mission_id: str) -> Optional[SimplifiedPlan]:
        """Retrieves the current plan for a mission."""
        mission = self.get_mission_context(mission_id)
        if mission:
            return mission.plan
        else:
            logger.warning(f"Cannot get plan for non-existent mission ID: {mission_id}")
            return None
            
    async def update_mission_metadata(self, mission_id: str, metadata_update: Dict[str, Any]):
        """Updates mission metadata and persists the updated context to the database."""
        mission = self.get_mission_context(mission_id)
        if mission:
            mission.metadata.update(metadata_update)
            mission.update_timestamp()
            
            async with get_async_db() as db:
                try:
                    sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                    await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                    logger.debug(f"Updated metadata for mission {mission_id} with keys: {list(metadata_update.keys())} and updated DB.")
                except Exception as e:
                    logger.error(f"Database error updating metadata for mission {mission_id}: {e}", exc_info=True)
        else:
            logger.error(f"Cannot update metadata for non-existent mission ID: {mission_id}")

    # --- New Method for Logging Execution Steps ---
    async def log_execution_step(
        self,
        mission_id: str,
        agent_name: str,
        action: str,
        input_summary: Optional[str] = None,
        output_summary: Optional[str] = None,
        status: Literal["success", "failure", "warning", "running"] = "success", # <-- Updated status literal
        error_message: Optional[str] = None,
        # --- Added parameters for detailed logging ---
        full_input: Optional[Any] = None,
        full_output: Optional[Any] = None,
        model_details: Optional[Dict[str, Any]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        file_interactions: Optional[List[str]] = None,
        # --- End added parameters ---
        log_queue: Optional[queue.Queue] = None, # <-- Add queue parameter
        update_callback: Optional[Callable[[queue.Queue, ExecutionLogEntry], None]] = None # <-- Modify callback signature
    ):
        """Logs a step in the mission execution process and optionally calls a callback with the queue."""
        mission = self.get_mission_context(mission_id)
        
        # Check if mission is paused/stopped before logging
        # BUT allow pause/stop actions themselves to be logged
        if mission and mission.status in ["paused", "stopped"]:
            # Allow pause/resume/stop actions to be logged even when status is paused/stopped
            if action not in ["Pause Mission", "Stop Mission", "Resume Mission"]:
                logger.debug(f"Skipping log for {agent_name}/{action} - mission {mission_id} is {mission.status}")
                return  # Don't log if mission is paused/stopped (except for pause/stop/resume actions)
        
        if mission:
            # --- Make detailed fields serializable and sanitized BEFORE creating ExecutionLogEntry ---
            serializable_input = sanitize_for_jsonb(_make_serializable(full_input)) if full_input else None
            serializable_output = sanitize_for_jsonb(_make_serializable(full_output)) if full_output else None
            serializable_model_details = sanitize_for_jsonb(_make_serializable(model_details)) if model_details else None
            serializable_tool_calls = sanitize_for_jsonb(_make_serializable(tool_calls)) if tool_calls else None
            # file_interactions is already List[str], but sanitize it too
            file_interactions = sanitize_for_jsonb(file_interactions) if file_interactions else None
            # --- End serialization and sanitization step ---

            try: # Add try-except around ExecutionLogEntry creation for robustness
                log_entry = ExecutionLogEntry(
                    agent_name=agent_name,
                    action=action,
                    input_summary=input_summary,
                    output_summary=output_summary,
                    status=status,
                    error_message=error_message,
                    # --- Pass SERIALIZED detailed fields ---
                    full_input=serializable_input,
                    full_output=serializable_output,
                    model_details=serializable_model_details,
                    tool_calls=serializable_tool_calls,
                    file_interactions=file_interactions
                    # --- End detailed fields ---
                )
            except ValidationError as ve:
                 logger.error(f"Pydantic validation error creating ExecutionLogEntry for mission {mission_id}: {ve}", exc_info=True)
                 # Fallback: Create a minimal log entry
                 log_entry = ExecutionLogEntry(
                     agent_name=agent_name,
                     action=action,
                     input_summary=input_summary,
                     output_summary=output_summary,
                     status="failure", # Mark as failure due to logging issue
                     error_message=f"Failed to create detailed log entry: {ve}",
                 )
            except Exception as e:
                 logger.error(f"Unexpected error creating ExecutionLogEntry for mission {mission_id}: {e}", exc_info=True)
                 # Fallback: Create a minimal log entry
                 log_entry = ExecutionLogEntry(
                     agent_name=agent_name,
                     action=action,
                     input_summary=input_summary,
                     output_summary=output_summary,
                     status="failure", # Mark as failure due to logging issue
                     error_message=f"Unexpected error creating log entry: {e}",
                 )


            mission.execution_log.append(log_entry)
            mission.update_timestamp()
            logger.info(f"Logged execution step for mission {mission_id}: Agent={agent_name}, Action={action}, Status={status}")

            # Persist the log entry to the database using the new execution logs table
            async with get_async_db() as db:
                try:
                    # Get the mission without user constraint to get the user_id
                    mission_db = await crud.get_mission(db, mission_id=mission_id)
                    if mission_db:
                        # Extract cost and token information from model_details
                        cost = None
                        prompt_tokens = None
                        completion_tokens = None
                        native_tokens = None
                        
                        if log_entry.model_details:
                            cost = log_entry.model_details.get('cost')
                            prompt_tokens = log_entry.model_details.get('prompt_tokens')
                            completion_tokens = log_entry.model_details.get('completion_tokens')
                            native_tokens = log_entry.model_details.get('native_total_tokens')
                            
                            # Also try alternative field names that might be used
                            if cost is None:
                                cost = log_entry.model_details.get('total_cost')
                            if native_tokens is None:
                                native_tokens = log_entry.model_details.get('total_tokens')
                        
                        # Debug logging to see what we're actually saving
                        logger.debug(f"Saving execution log to DB for mission {mission_id}:")
                        logger.debug(f"  - cost: {cost} (from model_details: {log_entry.model_details.get('cost') if log_entry.model_details else 'N/A'})")
                        logger.debug(f"  - prompt_tokens: {prompt_tokens}")
                        logger.debug(f"  - completion_tokens: {completion_tokens}")
                        logger.debug(f"  - native_tokens: {native_tokens}")
                        logger.debug(f"  - model_details keys: {list(log_entry.model_details.keys()) if log_entry.model_details else 'None'}")
                        
                        # Get chat to get user_id
                        chat = await crud.get_chat(db, chat_id=mission_db.chat_id, user_id=1)  # Need to get user_id properly
                        user_id = chat.user_id if chat else 1
                        
                        # Sanitize all JSONB fields before saving to database
                        sanitized_full_input = sanitize_for_jsonb(log_entry.full_input) if log_entry.full_input else None
                        sanitized_full_output = sanitize_for_jsonb(log_entry.full_output) if log_entry.full_output else None
                        sanitized_model_details = sanitize_for_jsonb(log_entry.model_details) if log_entry.model_details else None
                        sanitized_tool_calls = sanitize_for_jsonb(log_entry.tool_calls) if log_entry.tool_calls else None
                        sanitized_file_interactions = sanitize_for_jsonb(log_entry.file_interactions) if log_entry.file_interactions else None
                        
                        # Create execution log entry in database - matching sync version signature
                        await crud.create_execution_log(
                            db=db,
                            mission_id=mission_id,
                            timestamp=log_entry.timestamp,
                            agent_name=log_entry.agent_name,
                            action=log_entry.action,
                            input_summary=log_entry.input_summary,
                            output_summary=log_entry.output_summary,
                            status=log_entry.status,
                            error_message=log_entry.error_message,
                            full_input=sanitized_full_input,
                            full_output=sanitized_full_output,
                            model_details=sanitized_model_details,
                            tool_calls=sanitized_tool_calls,
                            file_interactions=sanitized_file_interactions,
                            cost=cost,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            native_tokens=native_tokens
                        )
                        logger.debug(f"Persisted execution log entry to database for mission {mission_id}")
                    else:
                        logger.error(f"Could not find mission {mission_id} in database to persist execution log")
                    
                    # Also update the mission context (for backward compatibility)
                    sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                    await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                except Exception as e:
                    logger.error(f"Database error saving execution log for mission {mission_id}: {e}", exc_info=True)
            
            # ALWAYS send WebSocket update for execution log
            # Even if callback is provided, we send the update directly to ensure it's not lost
            try:
                # Convert log entry to dict for WebSocket
                log_dict = {
                    "log_id": log_entry.log_id,  # Include the unique log ID
                    "timestamp": log_entry.timestamp.isoformat() if hasattr(log_entry.timestamp, 'isoformat') else str(log_entry.timestamp),
                    "agent_name": log_entry.agent_name,
                    "message": log_entry.action,
                    "action": log_entry.action,
                    "input_summary": log_entry.input_summary,
                    "output_summary": log_entry.output_summary,
                    "status": log_entry.status,
                    "error_message": log_entry.error_message,
                    "full_input": log_entry.full_input,
                    "full_output": log_entry.full_output,
                    "model_details": log_entry.model_details,
                    "tool_calls": log_entry.tool_calls,
                    "file_interactions": log_entry.file_interactions
                }
                _send_websocket_update(send_logs_update(mission_id, [log_dict], "append"))
                logger.debug(f"Sent execution log update via WebSocket for mission '{mission_id}'.")
            except Exception as ws_error:
                logger.error(f"Failed to send execution log update via WebSocket for mission {mission_id}: {ws_error}")

            # Call the callback function if provided, passing the queue and log entry
            if update_callback and log_queue is not None:
                try:
                    logger.debug(f"Sending log entry '{log_entry.action}' for agent '{log_entry.agent_name}' to frontend via WebSocket.")
                    # Pass a deep copy to avoid potential issues if the callback modifies the entry
                    update_callback(log_queue, log_entry.model_copy(deep=True))
                except Exception as cb_e:
                    logger.error(f"Error executing update callback for mission {mission_id}: {cb_e}", exc_info=True)
            elif update_callback and log_queue is None:
                 logger.warning(f"log_execution_step called with update_callback but no log_queue for mission {mission_id}. Callback skipped.")
        else:
            logger.error(f"Cannot log execution step for non-existent mission ID: {mission_id}")


    # --- Goal Pad Management Methods ---

    async def add_goal(self, mission_id: str, text: str, source_agent: Optional[str] = None) -> Optional[str]:
        """Adds a new goal and persists the updated context to the database."""
        mission = self.get_mission_context(mission_id)
        if mission:
            try:
                new_goal = GoalEntry(text=text, source_agent=source_agent)
                mission.goal_pad.append(new_goal)
                mission.update_timestamp()
                
                async with get_async_db() as db:
                    try:
                        sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                        await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                        logger.info(f"Added goal '{new_goal.goal_id}' to mission {mission_id} and updated DB.")
                        
                        # Send WebSocket update for goal pad
                        try:
                            goals_list = [goal.model_dump() for goal in mission.goal_pad]
                            _send_websocket_update(send_goal_pad_update(mission_id, goals_list, "update"))
                            logger.info(f"Sent goal pad update via WebSocket for mission '{mission_id}'.")
                        except Exception as ws_error:
                            logger.error(f"Failed to send goal pad update via WebSocket for mission {mission_id}: {ws_error}")
                    except Exception as e:
                        logger.error(f"Database error adding goal for mission {mission_id}: {e}", exc_info=True)

                return new_goal.goal_id
            except ValidationError as e:
                logger.error(f"Validation error creating GoalEntry for mission {mission_id}: {e}")
                return None
            except Exception as e:
                logger.error(f"Unexpected error adding goal for mission {mission_id}: {e}", exc_info=True)
                return None
        else:
            logger.error(f"Cannot add goal for non-existent mission ID: {mission_id}")
            return None

    async def update_goal_status(self, mission_id: str, goal_id: str, status: Literal["active", "addressed", "obsolete"]) -> bool:
        """Updates a goal's status and persists the updated context to the database."""
        mission = self.get_mission_context(mission_id)
        if not mission:
            logger.error(f"Cannot update goal status for non-existent mission ID: {mission_id}")
            return False

        goal_found = False
        should_update_db = False
        for goal in mission.goal_pad:
            if goal.goal_id == goal_id:
                if goal.status != status:
                    goal.status = status
                    mission.update_timestamp()
                    should_update_db = True
                    logger.info(f"Updated status of goal '{goal_id}' to '{status}' for mission {mission_id}.")
                else:
                    logger.debug(f"Goal '{goal_id}' status already '{status}' for mission {mission_id}. No update needed.")
                goal_found = True
                break
        
        if not goal_found:
            logger.warning(f"Goal '{goal_id}' not found in goal_pad for mission {mission_id}. Cannot update status.")
            return False

        if should_update_db:
            async with get_async_db() as db:
                try:
                    sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                    await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                except Exception as e:
                    logger.error(f"Database error updating goal status for mission {mission_id}: {e}", exc_info=True)
        
        return True

    async def edit_goal_text(self, mission_id: str, goal_id: str, new_text: str) -> bool:
        """Updates a goal's text and persists the updated context to the database."""
        mission = self.get_mission_context(mission_id)
        if not mission:
            logger.error(f"Cannot update goal text for non-existent mission ID: {mission_id}")
            return False

        goal_found = False
        should_update_db = False
        for goal in mission.goal_pad:
            if goal.goal_id == goal_id:
                if goal.text != new_text:
                    goal.text = new_text
                    mission.update_timestamp()
                    should_update_db = True
                    logger.info(f"Updated text of goal '{goal_id}' for mission {mission_id}.")
                else:
                    logger.debug(f"Goal '{goal_id}' text unchanged for mission {mission_id}. No update needed.")
                goal_found = True
                break

        if not goal_found:
            logger.warning(f"Goal '{goal_id}' not found in goal_pad for mission {mission_id}. Cannot update text.")
            return False

        if should_update_db:
            async with get_async_db() as db:
                try:
                    sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                    await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                except Exception as e:
                    logger.error(f"Database error editing goal text for mission {mission_id}: {e}", exc_info=True)

        return True

    def get_goal_pad(self, mission_id: str) -> List[GoalEntry]:
        """Retrieves the full goal pad for a given mission ID."""
        mission = self.get_mission_context(mission_id)
        if mission:
            return mission.goal_pad
        else:
            logger.warning(f"Cannot get goal_pad for non-existent mission ID: {mission_id}")
            return []

    def get_active_goals(self, mission_id: str) -> List[GoalEntry]:
        """Retrieves only the active goals from the goal pad for a given mission ID."""
        mission = self.get_mission_context(mission_id)
        if mission:
            return [goal for goal in mission.goal_pad if goal.status == "active"]
        else:
            logger.warning(f"Cannot get active goals for non-existent mission ID: {mission_id}")
            return []

    # --- End Goal Pad Management Methods ---


    # --- Thought Pad Management Methods ---

    async def add_thought(self, mission_id: str, agent_name: str, content: str) -> Optional[str]:
        """Adds a new thought and persists the updated context to the database."""
        mission = self.get_mission_context(mission_id)
        if mission:
            try:
                new_thought = ThoughtEntry(agent_name=agent_name, content=content)
                mission.thought_pad.append(new_thought)
                mission.update_timestamp()

                async with get_async_db() as db:
                    try:
                        sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                        await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                        logger.info(f"Added thought '{new_thought.thought_id}' from agent '{agent_name}' to mission {mission_id} and updated DB.")
                        
                        # Send WebSocket update for thought pad
                        try:
                            thoughts_list = [thought.model_dump() for thought in mission.thought_pad]
                            _send_websocket_update(send_thought_pad_update(mission_id, thoughts_list, "update"))
                            logger.info(f"Sent thought pad update via WebSocket for mission '{mission_id}'.")
                        except Exception as ws_error:
                            logger.error(f"Failed to send thought pad update via WebSocket for mission {mission_id}: {ws_error}")
                    except Exception as e:
                        logger.error(f"Database error adding thought for mission {mission_id}: {e}", exc_info=True)

                return new_thought.thought_id
            except ValidationError as e:
                logger.error(f"Validation error creating ThoughtEntry for mission {mission_id}: {e}")
                return None
            except Exception as e:
                logger.error(f"Unexpected error adding thought for mission {mission_id}: {e}", exc_info=True)
                return None
        else:
            logger.error(f"Cannot add thought for non-existent mission ID: {mission_id}")
            return None

    def get_recent_thoughts(self, mission_id: str, limit: int = 5) -> List[ThoughtEntry]:
        """Retrieves the most recent thoughts from the thought pad, up to the specified limit."""
        mission = self.get_mission_context(mission_id)
        if mission:
            # Return the last 'limit' thoughts. Slicing handles cases where len < limit.
            return mission.thought_pad[-limit:]
        else:
            logger.warning(f"Cannot get recent thoughts for non-existent mission ID: {mission_id}")
            return []

    # --- End Thought Pad Management Methods ---

    # --- Draft Building Method (Moved from AgentController) ---
    def build_draft_from_context(self, mission_id: str) -> Optional[str]:
        """
        Builds the full draft text from the stored report content, using the plan outline
        for structure and hierarchical numbering. Returns None if prerequisites are missing.
        """
        mission_context = self.get_mission_context(mission_id)
        if not mission_context or not mission_context.plan or not mission_context.report_content:
            logger.error(f"Cannot build draft: Mission context, plan, or report content missing for {mission_id}.")
            return None

        full_draft = ""
        report_outline = mission_context.plan.report_outline
        report_content_map = mission_context.report_content

        # Use recursive function to build draft with hierarchical numbering
        def build_draft_recursive(section_list: List[ReportSection], level: int = 1, prefix: str = ""):
            nonlocal full_draft
            for i, section in enumerate(section_list):
                # Calculate the number for the current section
                current_number = f"{prefix}{i + 1}"
                # Generate the heading markdown
                heading_marker = "#" * level
                # Prepend the number to the title in the heading
                full_draft += f"{heading_marker} {current_number}. {section.title}\n\n"
                # Get the content for the section
                content = report_content_map.get(section.section_id, f"[Content missing for section {section.section_id}]")
                full_draft += f"{content}\n\n"
                # Recursively call for subsections, passing the new prefix
                if section.subsections:
                    build_draft_recursive(section.subsections, level + 1, prefix=f"{current_number}.")

        # Initial call to the recursive function
        build_draft_recursive(report_outline)
        logger.info(f"Successfully built draft for mission {mission_id} from context.")
        return full_draft.strip()

    def get_mission_draft(self, mission_id: str) -> Optional[str]:
        """Retrieves the current draft of the report for a mission."""
        return self.build_draft_from_context(mission_id)
    # --- End Draft Building Method ---


    # --- Stats Management Methods (Moved from AgentController) ---

    def get_mission_stats(self, mission_id: str) -> Dict[str, float]:
        """Retrieves the current statistics for a given mission."""
        return self.mission_stats.get(mission_id, {
            "total_cost": 0.0,
            "total_prompt_tokens": 0.0,
            "total_completion_tokens": 0.0,
            "total_native_tokens": 0.0,
            "total_web_search_calls": 0
        }).copy() # Return a copy

    def increment_web_search_count(
        self,
        mission_id: str,
        log_queue: Optional[queue.Queue] = None,
        update_callback: Optional[Callable] = None
    ) -> None:
        """Increments the web search counter for a mission and updates stats."""
        if not mission_id:
            logger.warning("Cannot increment web search count: No mission_id provided")
            return
        web_search_cost = config.WEB_SEARCH_COST_PER_CALL
        model_details = {
            "web_search_count": 1,
            "cost": web_search_cost,
            # Generate a unique ID for this non-LLM stat update
            "call_id": f"web_search_{mission_id}_{time.time()}"
        }
        # Schedule the async update
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(self.update_mission_stats(mission_id, model_details, log_queue, update_callback, force_update=True))
        except RuntimeError:
            # We're in a sync context, run it in a thread
            import threading
            def run_async_update():
                asyncio.run(self.update_mission_stats(mission_id, model_details, log_queue, update_callback, force_update=True))
            thread = threading.Thread(target=run_async_update, daemon=True)
            thread.start()
        logger.debug(f"Incremented web search count and added cost ${web_search_cost:.4f} for mission {mission_id}")

    async def update_mission_stats(
        self,
        mission_id: Optional[str],
        model_details: Optional[Dict[str, Any]],
        log_queue: Optional[queue.Queue] = None,
        update_callback: Optional[Callable] = None,
        force_update: bool = False
        ):
        """
        Updates the cumulative cost and token counts for a given mission and sends update via callback.
        Handles both prompt/completion tokens and native_total_tokens for complete tracking.
        Also persists the updated stats to the database.
        """
        if not mission_id or not model_details:
            return

        call_id = model_details.get("call_id")
        if not call_id and not force_update:
            timestamp = model_details.get("timestamp", time.time())
            duration = model_details.get("duration_sec", 0)
            model_name = model_details.get("model_name", "unknown")
            call_id = f"{model_name}_{timestamp}_{duration}"
            model_details["call_id"] = call_id

        if not force_update and call_id in self.tracked_calls:
            logger.debug(f"Skipping duplicate stats update for call {call_id} in mission {mission_id}")
            return

        if call_id:
            self.tracked_calls.add(call_id)

        cost = model_details.get("cost")
        prompt_tokens = model_details.get("prompt_tokens")
        completion_tokens = model_details.get("completion_tokens")
        native_total_tokens = model_details.get("native_total_tokens")
        web_search_count = model_details.get("web_search_count", 0)

        if cost is None and prompt_tokens is None and completion_tokens is None and native_total_tokens is None and web_search_count == 0:
            return

        stats = self.mission_stats.setdefault(mission_id, {
            "total_cost": 0.0,
            "total_prompt_tokens": 0.0,
            "total_completion_tokens": 0.0,
            "total_native_tokens": 0.0,
            "total_web_search_calls": 0
        })

        cost_increment = float(cost) if cost is not None else 0.0
        prompt_increment = float(prompt_tokens) if prompt_tokens is not None else 0.0
        completion_increment = float(completion_tokens) if completion_tokens is not None else 0.0
        native_increment = float(native_total_tokens) if native_total_tokens is not None else 0.0
        web_search_increment = int(web_search_count)

        stats["total_cost"] += cost_increment
        stats["total_prompt_tokens"] += prompt_increment
        stats["total_completion_tokens"] += completion_increment
        stats["total_web_search_calls"] += web_search_increment

        if native_increment > 0 and prompt_increment == 0 and completion_increment == 0:
            stats["total_native_tokens"] += native_increment
        elif prompt_increment > 0 or completion_increment > 0:
            stats["total_native_tokens"] = stats["total_prompt_tokens"] + stats["total_completion_tokens"]

        logger.debug(
            f"Updated stats for mission {mission_id}: "
            f"Cost +{cost_increment:.6f}, Prompt +{prompt_increment:.0f}, Completion +{completion_increment:.0f}, "
            f"Native +{native_increment:.0f}, Web Searches +{web_search_increment}. "
            f"New Total: Cost=${stats['total_cost']:.6f}, Prompt={stats['total_prompt_tokens']:.0f}, "
            f"Completion={stats['total_completion_tokens']:.0f}, Native={stats['total_native_tokens']:.0f}, "
            f"Web Searches={stats['total_web_search_calls']}"
        )
        
        # Create log entry for non-agent API calls that are MISSION-SPECIFIC
        # These don't use base_agent so won't create their own logs
        # We ONLY log mission-related activities, NOT application-level operations
        # Skip if agent already logged this call
        agent_logged = model_details.get("agent_logged", False) if model_details else False
        
        if cost_increment > 0 and mission_id and log_queue and update_callback and not agent_logged:
            agent_mode = model_details.get("agent_mode", "unknown")
            model_name = model_details.get("model_name", "unknown")
            
            # MISSION-SPECIFIC modes that need logging
            # These are operations that are part of mission execution
            # NOTE: "writing" mode removed - WritingAgent and report_generator handle their own logging
            non_agent_modes = {
                "query_preparation": ("QueryPreparer", "Query Preparation"),
                "router": ("Router", "Routing Decision"),
                "query_strategy": ("QueryStrategy", "Strategy Selection"),
                # "writing" removed - handled by WritingAgent and report_generator
            }
            
            # APPLICATION-LEVEL modes we explicitly IGNORE:
            # - "messenger" from chat_title_service (UI chat titles)
            # - "writing" from writing_controller (no mission context)
            # - Any other non-mission operations
            
            if agent_mode in non_agent_modes:
                logger.info(f"NON_AGENT_LOG: Creating log for {agent_mode} with cost ${cost_increment:.6f}")
                agent_name, action = non_agent_modes[agent_mode]
                
                # Special handling for "writing" mode - only log if it's mission-related
                if agent_mode == "writing":
                    # Check if this is from report_generator (has mission context)
                    # If no mission_id, it's probably from writing_controller (ignore)
                    if not mission_id:
                        return  # Skip logging for non-mission writing operations
                
                await self.log_execution_step(
                    mission_id=mission_id,
                    agent_name=agent_name,
                    action=action,
                    input_summary=f"Model: {model_name}",
                    output_summary=f"Tokens: {int(prompt_increment + completion_increment)}",
                    status="success",
                    model_details=model_details,
                    log_queue=log_queue,
                    update_callback=update_callback
                )
                logger.info(f"NON_AGENT_LOG: Successfully created log entry for {agent_mode}")
            else:
                logger.debug(f"NON_AGENT_LOG: Skipping {agent_mode} - not in non_agent_modes list")
        
        
        # Update the mission context with the new stats
        mission = self.get_mission_context(mission_id)
        if mission:
            # Update mission context fields with the accumulated stats
            mission.total_cost = stats["total_cost"]
            mission.total_tokens = {
                "prompt": int(stats["total_prompt_tokens"]),
                "completion": int(stats["total_completion_tokens"]),
                "native": int(stats["total_native_tokens"])
            }
            mission.total_web_searches = stats["total_web_search_calls"]
            mission.update_timestamp()
            
            # Persist to database asynchronously
            # logger.info(f"COST_DB_UPDATE: Saving stats to DB for mission {mission_id}: Cost=${stats['total_cost']:.6f}")
            async def save_stats_to_db():
                async with get_async_db() as db:
                    try:
                        sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                        await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                        # logger.info(f"COST_DB_UPDATE: Successfully saved stats to database for mission {mission_id}: Total Cost=${stats['total_cost']:.6f}")
                    except Exception as e:
                        logger.error(f"COST_DB_UPDATE: Failed to save stats to database for mission {mission_id}: {e}", exc_info=True)
            
            # Schedule the database update
            try:
                loop = asyncio.get_running_loop()
                asyncio.create_task(save_stats_to_db())
            except RuntimeError:
                # We're in a sync context, need to run it differently
                import threading
                def run_async_save():
                    asyncio.run(save_stats_to_db())
                thread = threading.Thread(target=run_async_save, daemon=True)
                thread.start()

        if log_queue and update_callback and (
            cost_increment > 0 or prompt_increment > 0 or completion_increment > 0 or
            native_increment > 0 or web_search_increment > 0
        ):
            try:
                stats_update_message = {
                    "type": "stats_update",
                    "mission_id": mission_id,
                    "payload": stats.copy() # Send a copy of the current totals
                }
                # Pass only the queue and the message payload, as the callback
                # being invoked here is the 2-argument wrapper in some cases.
                update_callback(log_queue, stats_update_message)
                logger.debug(f"Sent stats_update message to UI for mission {mission_id}")
            except Exception as e:
                logger.error(f"Failed to send stats_update message via callback: {e}", exc_info=True)

    # --- End Stats Management Methods ---

    # Track processed documents per mission to avoid re-processing
    _processed_documents_per_mission = {}

    async def _process_note_for_document_group(self, mission_id: str, note: Note):
        """
        Process a note to save web content as a document if auto_create_document_group is enabled.
        For database documents, add them to the document group.
        """
        try:
            mission = self.get_mission_context(mission_id)
            if not mission or not mission.metadata:
                return
            
            # Check if auto_create_document_group is enabled
            metadata = mission.metadata
            research_params = metadata.get("research_params", {})
            if not research_params.get("auto_create_document_group"):
                return
            
            # Get the document group ID
            group_id = metadata.get("generated_document_group_id")
            if not group_id:
                logger.warning(f"auto_create_document_group is enabled but no document group ID found for mission {mission_id}")
                return
            
            # Only process relevant notes
            if hasattr(note, 'is_relevant') and not note.is_relevant:
                return
            
            source_type = note.source_type if hasattr(note, 'source_type') else None
            source_id = note.source_id if hasattr(note, 'source_id') else None
            
            if not source_type or not source_id:
                return
            
            # Initialize processed documents set for this mission if needed
            if mission_id not in self._processed_documents_per_mission:
                self._processed_documents_per_mission[mission_id] = set()
            
            # Create a unique key for this source
            source_key = f"{source_type}:{source_id}"
            
            # Check if we've already processed this document in this mission
            if source_key in self._processed_documents_per_mission[mission_id]:
                logger.debug(f"Already processed {source_key} in mission {mission_id}, skipping")
                return
            
            # Mark as processed
            self._processed_documents_per_mission[mission_id].add(source_key)
            
            # Import database modules
            from database.database import get_db
            from database import crud, models
            import hashlib
            import uuid as uuid_lib
            
            if source_type == "web":
                # For web sources, save the full content as a document
                logger.info(f"Processing web note for document group: {source_id}")
                
                # Check if the note has fetched_full_content flag in metadata
                if hasattr(note, 'source_metadata') and note.source_metadata:
                    # source_metadata might be a dict or an object
                    metadata = note.source_metadata
                    fetched_full = False
                    
                    if isinstance(metadata, dict):
                        fetched_full = metadata.get('fetched_full_content', False)
                        logger.debug(f"Web note metadata (dict): fetched_full_content={fetched_full}, keys={metadata.keys() if metadata else 'None'}")
                    else:
                        fetched_full = getattr(metadata, 'fetched_full_content', False)
                        logger.debug(f"Web note metadata (obj): fetched_full_content={fetched_full}")
                    
                    if fetched_full:
                        # We have full content, save it as a document
                        db = next(get_db())
                        try:
                            # Generate a consistent UUID based on URL hash (not mission-specific)
                            # This ensures the same URL always maps to the same document ID
                            # Use UUID v5 with URL namespace to generate deterministic UUID from URL
                            import uuid
                            url_namespace = uuid.UUID('6ba7b811-9dad-11d1-80b4-00c04fd430c8')  # URL namespace
                            doc_id = str(uuid.uuid5(url_namespace, source_id))
                            
                            # Also generate a content hash for deduplication
                            url_hash = hashlib.sha256(source_id.encode()).hexdigest()
                            
                            # Get user_id from the mission's chat
                            # We need to query the mission directly without user_id filter since we don't have it yet
                            mission_db = db.query(models.Mission).filter(models.Mission.id == mission_id).first()
                            if not mission_db:
                                logger.error(f"Mission {mission_id} not found in database")
                                return
                            
                            # Get the chat to find the user_id
                            chat_db = db.query(models.Chat).filter(models.Chat.id == mission_db.chat_id).first()
                            if not chat_db:
                                logger.error(f"Chat {mission_db.chat_id} not found in database")
                                return
                            
                            user_id = chat_db.user_id
                            
                            # Check if document already exists
                            existing_doc = crud.get_document(db, doc_id=doc_id, user_id=user_id)
                            if not existing_doc:
                                # Create document entry
                                # Get title from metadata
                                if isinstance(metadata, dict):
                                    title = metadata.get('title', f"Web: {source_id[:50]}")
                                else:
                                    title = getattr(metadata, 'title', f"Web: {source_id[:50]}")
                                
                                # Get the full content - check if we have the full fetched content in metadata
                                # The note.content is the synthesized/summarized version
                                # We want the actual full page content if available
                                content = note.content if hasattr(note, 'content') else ""
                                
                                # Check if we have the full page text stored in metadata
                                if isinstance(metadata, dict):
                                    full_text = metadata.get('full_text', None)
                                else:
                                    full_text = getattr(metadata, 'full_text', None)
                                
                                if full_text:
                                    content = full_text
                                    logger.info(f"Using full fetched text for document {doc_id} ({len(content)} chars)")
                                
                                # Create markdown file with the content
                                import pathlib
                                markdown_dir = pathlib.Path("/app/data/markdown_files")
                                markdown_dir.mkdir(parents=True, exist_ok=True)
                                markdown_path = markdown_dir / f"{doc_id}.md"
                                
                                with open(markdown_path, 'w', encoding='utf-8') as f:
                                    f.write(f"# {title}\n\n")
                                    f.write(f"Source: {source_id}\n\n")
                                    f.write(content)
                                
                                # Create document record
                                now = crud.get_current_time()
                                # For original_filename, use a .md extension so processor knows it's markdown
                                # Store the actual URL in metadata
                                original_filename_for_processor = f"{doc_id}_web_document.md"
                                web_doc = models.Document(
                                    id=doc_id,
                                    user_id=user_id,
                                    filename=title,  # Use filename instead of title
                                    original_filename=original_filename_for_processor,  # Use .md extension for processor
                                    file_path=str(markdown_path),
                                    markdown_path=str(markdown_path),
                                    processing_status="pending",  # Set to pending so background processor picks it up
                                    created_at=now,
                                    updated_at=now,
                                    metadata_={  # Use metadata_ field name
                                        "url": source_id,  # Store actual URL in metadata
                                        "source": "mission_web_search",
                                        "mission_id": mission_id,
                                        "auto_captured": True,
                                        "first_captured_by_mission": mission_id,
                                        "content_hash": url_hash,  # Store hash in metadata
                                        "title": title,  # Store the extracted title
                                        "original_url": source_id  # Also store URL here for clarity
                                    }
                                )
                                db.add(web_doc)
                                
                                # Add to document group
                                document_group = crud.get_document_group(db, group_id=group_id, user_id=user_id)
                                if document_group:
                                    document_group.documents.append(web_doc)
                                    logger.info(f"Added new web document {doc_id} to document group {group_id}")
                                
                                db.commit()
                                logger.info(f"Created and saved web document {doc_id} for URL {source_id}")
                                logger.info(f"Document {doc_id} queued for background processing (status=pending)")
                            else:
                                logger.info(f"Web document {doc_id} already exists for URL {source_id}, reusing it")
                                
                                # Document already exists, just add to group if not already there
                                document_group = crud.get_document_group(db, group_id=group_id, user_id=user_id)
                                if document_group:
                                    # Check if document is already in the group
                                    if existing_doc not in document_group.documents:
                                        document_group.documents.append(existing_doc)
                                        db.commit()
                                        logger.info(f"Added existing web document {doc_id} to document group {group_id}")
                                    else:
                                        logger.debug(f"Web document {doc_id} is already in document group {group_id}, skipping")
                        except Exception as e:
                            logger.error(f"Failed to save web document for {source_id}: {e}", exc_info=True)
                            db.rollback()
                        finally:
                            db.close()
                    else:
                        logger.warning(f"Web note for {source_id} doesn't have fetched_full_content=True flag, skipping document creation")
                else:
                    logger.warning(f"Web note for {source_id} has no source_metadata, skipping document creation")
                        
            elif source_type == "document":
                # For database documents, just add them to the group
                logger.info(f"Processing document note for document group: {source_id}")
                
                # Extract document ID from source_id or metadata
                doc_id = None
                if hasattr(note, 'source_metadata') and note.source_metadata:
                    if hasattr(note.source_metadata, 'doc_id') and note.source_metadata.doc_id:
                        doc_id = note.source_metadata.doc_id
                    elif '_' in source_id:
                        # Try to extract doc_id from chunk_id format
                        doc_id = source_id.split('_')[0]
                
                if doc_id:
                    db = next(get_db())
                    try:
                        # Get user_id from the mission's chat
                        # We need to query the mission directly without user_id filter since we don't have it yet
                        mission_db = db.query(models.Mission).filter(models.Mission.id == mission_id).first()
                        if not mission_db:
                            return
                        
                        # Get the chat to find the user_id
                        chat_db = db.query(models.Chat).filter(models.Chat.id == mission_db.chat_id).first()
                        if not chat_db:
                            return
                        
                        user_id = chat_db.user_id
                        
                        # Get the document and add to group
                        document = crud.get_document(db, doc_id=doc_id, user_id=user_id)
                        if document:
                            document_group = crud.get_document_group(db, group_id=group_id, user_id=user_id)
                            if document_group:
                                # Check if document is already in the group
                                if document not in document_group.documents:
                                    document_group.documents.append(document)
                                    db.commit()
                                    logger.info(f"Added document {doc_id} to document group {group_id}")
                                else:
                                    logger.debug(f"Document {doc_id} is already in document group {group_id}, skipping")
                        else:
                            logger.warning(f"Document {doc_id} not found for user {user_id}")
                    except Exception as e:
                        logger.error(f"Failed to add document {doc_id} to group: {e}", exc_info=True)
                        db.rollback()
                    finally:
                        db.close()
                        
        except Exception as e:
            logger.error(f"Error processing note for document group: {e}", exc_info=True)
    
    def cleanup_mission_document_cache(self, mission_id: str):
        """Clean up the processed documents cache for a mission when it completes."""
        if mission_id in self._processed_documents_per_mission:
            del self._processed_documents_per_mission[mission_id]
            logger.debug(f"Cleaned up document processing cache for mission {mission_id}")
    
    async def update_phase_display(self, mission_id: str, phase_info: Dict[str, Any]):
        """Update the current phase display information for UI."""
        mission = self.get_mission_context(mission_id)
        if mission:
            mission.current_phase_display = phase_info
            mission.update_timestamp()
            
            # Send WebSocket update for phase change
            try:
                from services.websocket_manager import get_websocket_manager
                ws_manager = get_websocket_manager()
                
                phase_update = {
                    "type": "phase_update",
                    "phase": phase_info.get("phase", "unknown"),
                    "details": phase_info
                }
                
                await ws_manager.send_to_mission(mission_id, phase_update)
                logger.debug(f"Sent phase update for mission {mission_id}: {phase_info}")
            except Exception as e:
                logger.error(f"Failed to send phase update via WebSocket: {e}")
            
            # Also persist to database
            async with get_async_db() as db:
                try:
                    sanitized_context = sanitize_for_jsonb(mission.model_dump(mode='json'))
                    await crud.update_mission_context(db, mission_id=mission_id, mission_context=sanitized_context)
                except Exception as e:
                    logger.error(f"Database error updating phase display for mission {mission_id}: {e}", exc_info=True)
