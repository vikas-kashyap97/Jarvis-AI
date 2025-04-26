import openai
import json
from typing import Dict, Optional, List
import os
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request, send_from_directory
import threading
import webbrowser
from flask_cors import CORS
import base64
import tempfile
import re # Added import
from flask_socketio import SocketIO

# --- Add Logging Import ---
from secretary.utilities.logging import log_user_message, log_agent_message, log_system_message, log_network_message, log_error, log_warning, log_api_request, log_api_response

# --- Add CV Parser Import ---
#from src.cv_parser.parser import CVParser 

# Initialize the OpenAI client with your API key
try:
    # Try loading from .env file first
    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    
    # Clean up the API key if it contains newlines or spaces
    if api_key:
        api_key = api_key.replace("\n", "").replace(" ", "").strip()
except ImportError:
    api_key = os.getenv("OPENAI_API_KEY")

client = openai.OpenAI(api_key=api_key)
if not client.api_key:
    raise ValueError("Please set OPENAI_API_KEY in environment variables or .env file")
    
log_system_message("OpenAI client initialized successfully")

# Add these constants at the top level
SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/gmail.modify'  # Upgrade to allow reading, message modification, but not account management
]
CLIENT_ID = '326841262964-7l2e8mmu3jinoshrh42k8at7qmouo38g.apps.googleusercontent.com'
TOKEN_FILE = 'token.pickle'

# Define task structure
class Task:
    def __init__(self, title: str, description: str, due_date: datetime, 
                 assigned_to: str, priority: str, project_id: str):
        self.title = title
        self.description = description
        self.due_date = due_date
        self.assigned_to = assigned_to
        self.priority = priority
        self.project_id = project_id
        self.completed = False
        self.id = f"task_{hash(title + assigned_to + str(due_date))}"
    
    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "due_date": self.due_date.isoformat(),
            "assigned_to": self.assigned_to,
            "priority": self.priority,
            "project_id": self.project_id,
            "completed": self.completed
        }
    
    def __str__(self):
        return f"{self.title} - Due: {self.due_date.strftime('%Y-%m-%d')} - Assigned to: {self.assigned_to}"

class Network:
    """
    Represents a network that connects various agents and manages communication and task distribution between them.
    
    Attributes:
        nodes (Dict[str, LLMNode]): A dictionary that maps node IDs to node instances.
        log_file (Optional[str]): Path to a log file where messages will be recorded. If None, logging is disabled.
        tasks (List[Task]): A list that stores tasks assigned to nodes.
    """

    def __init__(self, log_file: Optional[str] = None):
        """
        Initialize a new Network instance.
        
        Args:
            log_file (Optional[str]): The file path for logging messages. If provided, every message
                                      sent through the network will be appended to this file.
                                      
        The constructor sets up:
         - an empty dictionary 'nodes' to store registered nodes,
         - a log file path (if any),
         - an empty list 'tasks' to track tasks in the network.
        """
        
        self.nodes: Dict[str, LLMNode] = {}
        self.log_file = log_file
        self.tasks: List[Task] = []

    def register_node(self, node: 'LLMNode'):
        """
        Register a node with the network.
        
        This method adds the node to the network's internal dictionary using the node's unique identifier.
        It also sets the node's 'network' attribute to reference this Network instance, establishing a two-way link.
        
        Args:
            node (LLMNode): The node instance to register. The node must have a 'node_id' attribute.
            
        After registration, the node can participate in messaging and task management within the network.
        """
        
        self.nodes[node.node_id] = node
        node.network = self

    def send_message(self, sender_id: str, recipient_id: str, content: str):
        """
        Send a message from one node to another.
        
        This function first logs the outgoing message by calling a private logging function.
        Then, it checks if the recipient exists in the network's nodes dictionary:
          - If the recipient exists, the message is delivered by invoking the recipient's 'receive_message' method.
          - If the recipient does not exist, an error message is printed.
        
        Args:
            sender_id (str): Identifier of the node sending the message.
            recipient_id (str): Identifier of the recipient node.
            content (str): The message content to be transmitted.
            
        The function ensures that every message is logged and that message delivery occurs only if the
        target node is registered in the network.
        """

        # Log the message regardless of whether the recipient exists.
        self._log_message(sender_id, recipient_id, content)

        # Send the message if the recipient exists in the network's node list.
        if recipient_id in self.nodes:
            self.nodes[recipient_id].receive_message(content, sender_id)
        else:
            # Print an error message if recipient is not found.
            print(f"Node {recipient_id} not found in the network.")

    def _log_message(self, sender_id: str, recipient_id: str, content: str):
        """
        Log a message to a file if logging is enabled.
        
        This private helper method writes the details of the message in a formatted string to the
        specified log file. It appends the message so that previous logs are preserved.
        
        Args:
            sender_id (str): The identifier of the node that originated the message.
            recipient_id (str): The identifier of the target node.
            content (str): The textual content of the message.
            
        If no log file is specified (i.e., log_file is None), the message is not logged.
        """
        
        # Log using our new logging module
        log_network_message(sender_id, recipient_id, content)
        
        # Also preserve original file logging if configured
        if self.log_file:
            # Open the log file in append mode with UTF-8 encoding to handle any special characters.
            with open(self.log_file, "a", encoding="utf-8") as f:
                # Write the message in a readable format.
                f.write(f"From {sender_id} to {recipient_id}: {content}\n")
    
    def add_task(self, task: Task):
        """
        Add a new task to the network and notify the assigned node.
        
        The task is appended to the network's task list. If the task has an assigned node (its 'assigned_to' attribute
        corresponds to a registered node), the method constructs a notification message detailing the task's title,
        due date, and priority, and then sends this message from a system-generated sender.
        
        Args:
            task (Task): A task object with at least the following attributes:
                        - title (str): A brief description or title of the task.
                        - due_date (datetime): A datetime object representing the task's deadline.
                        - priority (Any): The priority level of the task.
                        - assigned_to (str): The node ID of the node to which the task is assigned.
                        
        This approach immediately informs the responsible node of new task assignments, which is critical
        for task management in networked applications.
        """
        
        self.tasks.append(task) # Add the new task to the list.
        
        # Build a notification message with task details.
        if task.assigned_to in self.nodes:
            message = f"New task assigned: {task.title}. Due: {task.due_date.strftime('%Y-%m-%d')}. Priority: {task.priority}."

            # Send the notification message from a system-originated sender.
            self.send_message("system", task.assigned_to, message)
    
    def get_tasks_for_node(self, node_id: str) -> List[Task]:
        """
        Retrieve all tasks assigned to a given node.
        
        This method filters the list of tasks and returns only those tasks where the 'assigned_to' attribute
        matches the provided node_id. This allows a node (or any client) to query for tasks specifically targeted to it.
        
        Args:
            node_id (str): The identifier of the node for which to fetch assigned tasks.
        
        Returns:
            List[Task]: A list of task objects that have been assigned to the node with the given node_id.
        """

        # Use a list comprehension to filter tasks by comparing the assigned_to attribute.
        return [task for task in self.tasks if task.assigned_to == node_id]


class LLMNode:
    def __init__(self, node_id: str, knowledge: str = "",
                 llm_api_key: str = "", llm_params: dict = None):
        """
        Initialize a new LLMNode instance.
        
        Each node represents an independent user/agent with its own knowledge base,
        project and calendar information, and configuration for the language model.
        
        Args:
            node_id (str): Unique identifier for this node.
            knowledge (str): Initial knowledge or context for the node.
            llm_api_key (str): API key for accessing the LLM. If empty, uses a shared client.
            llm_params (dict): Dictionary of LLM parameters such as model, temperature, and max_tokens.
            
        The constructor sets up:
          - Node identifier and base knowledge.
          - The LLM client (either shared or private based on API key).
          - Default LLM parameters if not provided.
          - Structures for conversation history, projects, calendar, and Google services.
          - A placeholder for network connection.
        """
                     
        self.node_id = node_id
        self.knowledge = knowledge

        # If an individual LLM API key is provided, initialize a new client using that key;
        # otherwise, fall back to a shared global 'client'
        self.llm_api_key = llm_api_key
        self.client = client if not self.llm_api_key else openai.OpenAI(api_key=self.llm_api_key)

        # Set LLM parameters with default values if none are provided
        self.llm_params = llm_params if llm_params else {
            "model": "gpt-4.1",
            "temperature": 0.1,
            "max_tokens": 1000
        }

        # Initialize an empty conversation history list to store chat messages
        self.conversation_history = []

        # Dictionary to store information about multiple projects; key is project ID (i.e. { project_id: {...}, ... })
        self.projects = {}

        # Local calendar list to store meeting information as dictionaries
        self.calendar = []
                     
        # Initialize Google services (Calendar, Gmail) using a helper function
        self.google_services = self._initialize_google_services()
        self.calendar_service = self.google_services.get('calendar')
        self.gmail_service = self.google_services.get('gmail')

        # Placeholder for network, set when the node is registered with a Network instance
        self.network: Optional[Network] = None

    def _initialize_google_services(self):
        """
        Initialize Google services (Calendar and Gmail) with shared authentication.
        
        This function performs the following steps:
          1. Check for a client secret in the environment.
          2. Attempt to load credentials from a token file.
          3. Refresh credentials if expired, or start a new OAuth flow if necessary.
          4. Save the new credentials.
          5. Build and test Google Calendar and Gmail services.
          
        Returns:
            dict: A dictionary with service objects for 'calendar' and 'gmail'. 
                  If initialization fails, the corresponding service remains None.
        """
        
        print(f"[{self.node_id}] Initializing Google services...")
        
        services = {'calendar': None, 'gmail': None}
        
        # Check for Google client secret from environment variables
        client_secret = os.getenv('GOOGLE_CLIENT_SECRET')
        if not client_secret:
            print(f"[{self.node_id}] ERROR: GOOGLE_CLIENT_SECRET environment variable not found")
            return services    # Cannot proceed without client secret
        
        print(f"[{self.node_id}] Client secret found: {client_secret[:5]}...")
        
        creds = None
        # Attempt to load stored credentials from TOKEN_FILE, if available
        if os.path.exists(TOKEN_FILE):
            print(f"[{self.node_id}] Found existing token file")
            try:
                with open(TOKEN_FILE, 'rb') as token:
                    creds = pickle.load(token)
                print(f"[{self.node_id}] Successfully loaded credentials from token file")
            except Exception as e:
                print(f"[{self.node_id}] Error loading token file: {str(e)}")
                # Remove the token file if it cannot be loaded
                os.remove(TOKEN_FILE)
                print(f"[{self.node_id}] Deleted invalid token file")
                creds = None
        else:
            print(f"[{self.node_id}] No token file found at {TOKEN_FILE}")
        
        try:
            # Refresh credentials if needed
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    try:
                        print(f"[{self.node_id}] Refreshing expired credentials")
                        creds.refresh(Request())
                        print(f"[{self.node_id}] Credentials refreshed successfully")
                    except Exception as e:
                        print(f"[{self.node_id}] Error refreshing credentials: {str(e)}")
                        print(f"[{self.node_id}] Will start new OAuth flow")
                        creds = None
                        if os.path.exists(TOKEN_FILE):
                            os.remove(TOKEN_FILE)
                            print(f"[{self.node_id}] Deleted invalid token file")

                # If no valid credentials exist, start a new OAuth flow
                if not creds:
                    print(f"[{self.node_id}] Starting new OAuth flow with client ID: {CLIENT_ID[:10]}...")
                    client_config = {
                        "installed": {
                            "client_id": CLIENT_ID,
                            "client_secret": client_secret,
                            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                            "token_uri": "https://oauth2.googleapis.com/token",
                            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                            "redirect_uris": ["http://localhost:8080/"]
                        }
                    }
                    
                    try:
                        flow = InstalledAppFlow.from_client_config(
                            client_config,
                            scopes=SCOPES,
                        )
                        print(f"[{self.node_id}] OAuth flow created successfully")
                        
                        # Open authorization URL for user consent in a web browser
                        auth_url, _ = flow.authorization_url(prompt='consent')
                        print(f"[{self.node_id}] Opening authorization URL in browser: {auth_url[:60]}...")
                        webbrowser.open(auth_url)
                        
                        print(f"[{self.node_id}] Running local server for authentication on port 8080...")
                        print(f"[{self.node_id}] Please complete the authorization in your browser")
                        creds = flow.run_local_server(port=8080)
                        print(f"[{self.node_id}] Authentication successful")
                    except Exception as e:
                        print(f"[{self.node_id}] Authentication error: {str(e)}")
                        print(f"[{self.node_id}] Full error details: {repr(e)}")
                        return services

                # Save the credentials for future use
                print(f"[{self.node_id}] Saving credentials to token file: {TOKEN_FILE}")
                try:
                    with open(TOKEN_FILE, 'wb') as token:
                        pickle.dump(creds, token)
                    print(f"[{self.node_id}] Credentials saved successfully")
                except Exception as e:
                    print(f"[{self.node_id}] Error saving credentials: {str(e)}")

            # Initialize the Google Calendar service
            try:
                print(f"[{self.node_id}] Building calendar service...")
                calendar_service = build('calendar', 'v3', credentials=creds)
                
                # Test the calendar service by fetching the calendar list
                calendar_list = calendar_service.calendarList().list().execute()
                print(f"[{self.node_id}] Calendar service working! Found {len(calendar_list.get('items', []))} calendars")
                services['calendar'] = calendar_service
            except Exception as e:
                print(f"[{self.node_id}] Failed to initialize Calendar service: {str(e)}")
            
            # Initialize the Gmail service
            try:
                print(f"[{self.node_id}] Building Gmail service...")
                gmail_service = build('gmail', 'v1', credentials=creds)
                
                # Test the Gmail service by fetching the user's profile
                profile = gmail_service.users().getProfile(userId='me').execute()
                print(f"[{self.node_id}] Gmail service working! Connected to {profile.get('emailAddress')}")
                services['gmail'] = gmail_service
            except Exception as e:
                print(f"[{self.node_id}] Failed to initialize Gmail service: {str(e)}")
            
            return services
            
        except Exception as e:
            print(f"[{self.node_id}] Failed to initialize Google services: {str(e)}")
            return services

    def create_calendar_reminder(self, task: Task):
        """
        Create a Google Calendar reminder for a given task.
        
        This method builds an event from the task details (title, due date, description, priority, etc.)
        and inserts the event using the calendar service.
        
        Args:
            task (Task): Task object with attributes: title, description, due_date, priority, project_id, assigned_to.
            
        If the calendar service is not available, it will log that and skip reminder creation.
        """
        
        if not self.calendar_service:
            print(f"[{self.node_id}] Calendar service not available, skipping reminder creation")
            return
            
        try:
            # Construct the event details in the format expected by Google Calendar
            event = {
                'summary': f"TASK: {task.title}",
                'description': f"{task.description}\n\nPriority: {task.priority}\nProject: {task.project_id}",
                'start': {
                    'dateTime': task.due_date.isoformat(),
                    'timeZone': 'UTC',
                },
                'end': {
                    'dateTime': (task.due_date + timedelta(hours=1)).isoformat(),
                    'timeZone': 'UTC',
                },
                'attendees': [{'email': f'{task.assigned_to}@example.com'}],
                'reminders': {
                    'useDefault': False,
                    'overrides': [
                        {'method': 'email', 'minutes': 24 * 60},  # 1 day before
                        {'method': 'popup', 'minutes': 60}         # 1 hour before
                    ]
                }
            }

            # Insert the event into the primary calendar
            event = self.calendar_service.events().insert(calendarId='primary', body=event).execute()
            print(f"[{self.node_id}] Task reminder created: {event.get('htmlLink')}")
            
        except Exception as e:
            print(f"[{self.node_id}] Failed to create calendar reminder: {e}")

    # Replace the local meeting scheduling with Google Calendar version
    def schedule_meeting(self, project_id: str, participants: list):
        """
        Schedule a meeting using Google Calendar.
        
        If the Google Calendar service is available, the meeting event is created with start and end times.
        If not, the method falls back to local scheduling.
        
        Args:
            project_id (str): Identifier for the project this meeting is associated with.
            participants (list): List of participant identifiers (usually email prefixes).
            
        The method also notifies other participants by adding the event to their local calendars and sending messages.
        """
        
        if not self.calendar_service:
            print(f"[{self.node_id}] Calendar service not available, using local scheduling")
            self._fallback_schedule_meeting(project_id, participants)
            return
            
        meeting_description = f"Meeting for project '{project_id}'"
        
        # Schedule meeting for one day later, for a duration of one hour
        start_time = datetime.now() + timedelta(days=1)
        end_time = start_time + timedelta(hours=1)
        
        # Build the meeting event structure
        event = {
            'summary': meeting_description,
            'start': {
                'dateTime': start_time.isoformat(),
                'timeZone': 'UTC',
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': 'UTC',
            },
            'attendees': [{'email': f'{p}@example.com'} for p in participants],
        }

        try:
            # Insert the meeting event into the calendar and capture the response event
            event = self.calendar_service.events().insert(calendarId='primary', body=event).execute()
            print(f"[{self.node_id}] Meeting created: {event.get('htmlLink')}")
            
            # Add meeting details to the node's local calendar
            self.calendar.append({
                'project_id': project_id,
                'meeting_info': meeting_description,
                'event_id': event['id']
            })

            # Notify each participant (except self) by adding event details to their local calendar and sending a message
            for p in participants:
                if p != self.node_id and p in self.network.nodes:
                    self.network.nodes[p].calendar.append({
                        'project_id': project_id,
                        'meeting_info': meeting_description,
                        'event_id': event['id']
                    })
                    notification = f"New meeting: '{meeting_description}' scheduled by {self.node_id} for {start_time.strftime('%Y-%m-%d %H:%M')}"
                    self.network.send_message(self.node_id, p, notification)
        except Exception as e:
            print(f"[{self.node_id}] Failed to create calendar event: {e}")
            # If creation fails, revert to local scheduling
            self._fallback_schedule_meeting(project_id, participants)
    
    # Uncomment the fallback method
    def _fallback_schedule_meeting(self, project_id: str, participants: list):
        """
        Fallback method to locally schedule a meeting when Google Calendar is unavailable.
        
        This method simply creates a textual record of the meeting and notifies participants.
        
        Args:
            project_id (str): Identifier for the project related to the meeting.
            participants (list): List of participant identifiers.
        """
        
        meeting_info = f"Meeting for project '{project_id}' scheduled for {datetime.now() + timedelta(days=1)}"
        self.calendar.append({
            'project_id': project_id,
            'meeting_info': meeting_info
        })
        
        print(f"[{self.node_id}] Scheduled local meeting: {meeting_info}")
        
        # Notify every participant in the network about the meeting
        for p in participants:
            if p in self.network.nodes:
                self.network.nodes[p].calendar.append({
                    'project_id': project_id,
                    'meeting_info': meeting_info
                })
                print(f"[{self.node_id}] Notified {p} about meeting for project '{project_id}'.")

    def receive_message(self, message: str, sender_id: str):
        """
        Process an incoming message to the node.
        
        This method handles various kinds of messages:
          - Direct messages and commands coming from the CLI (sender_id "cli_user").
          - Ongoing meeting scheduling context.
          - Regular conversation handling via the LLM.
        
        Args:
            message (str): The incoming message content.
            sender_id (str): The identifier of the sender.
        """
        
        # Log the incoming message
        if sender_id == "cli_user":
            log_user_message(sender_id, message)
        else:
            log_network_message(sender_id, self.node_id, message)
            
        print(f"[{self.node_id}] Received from {sender_id}: {message}")

        # --- Start: Added Command Parsing for UI/CLI ---
        if sender_id == "cli_user":
            # If message equals "tasks", list tasks and return immediately
            if message.strip().lower() == "tasks":
                tasks_list = self.list_tasks()
                # Ensure the response format matches what the UI expects
                print(f"[{self.node_id}] Response: {tasks_list}") 
                return # Stop further processing

            # Check for "plan" command using regex (e.g., "plan p1 = objective")
            plan_match = re.match(r"^\s*plan\s+([\w-]+)\s*=\s*(.+)$", message.strip(), re.IGNORECASE)
            if plan_match:
                project_id = plan_match.group(1).strip()
                objective = plan_match.group(2).strip()
                self.plan_project(project_id, objective) 
                return # Stop further processing after initiating the project plan
        # --- End: Command Parsing ---

        # If a meeting information-gathering is in progress, continue collecting meeting info
        if hasattr(self, 'meeting_context') and self.meeting_context.get('active'):
            self._continue_meeting_creation(message, sender_id)
            return # Stop further processing

        # Check if we're in the middle of email composition
        if hasattr(self, 'email_context') and self.email_context.get('active'):
            self._continue_email_composition(message, sender_id)
            return # Stop further processing

        # Regular message processing
        if sender_id == "cli_user":
            # Attempt to detect if the message is intended as a calendar command
            calendar_intent = self._detect_calendar_intent(message)
            if calendar_intent.get("is_calendar_command", False):
                action = calendar_intent.get("action")
                missing_info = calendar_intent.get("missing_info", [])
                
                if action == "schedule_meeting":
                    # If additional info is needed, start meeting creation flow
                    if missing_info:
                        self._start_meeting_creation(message, missing_info)
                    else:
                        self._handle_meeting_creation(message)
                    return
                elif action == "cancel_meeting":
                    self._handle_meeting_cancellation(message)
                    return
                elif action == "list_meetings":
                    self._handle_list_meetings()
                    return
                elif action == "reschedule_meeting":
                    self._handle_meeting_rescheduling(message)
                    return
            
            # Check if this is a send email request
            email_intent = self._detect_send_email_intent(message)
            
            if email_intent.get("is_send_email", False):
                missing_info = email_intent.get("missing_info", [])
                
                if missing_info:
                    # Start the email composition flow
                    self._start_email_composition(message, missing_info, email_intent)
                else:
                    # All information is already provided - show preview and ask for confirmation
                    self._start_email_composition(message, [], email_intent)
                return
            
            # Check if this is an email-related command using advanced detection
            email_analysis = self._analyze_email_command(message)
            
            if email_analysis.get("action") != "none":
                # Process email command with advanced handling
                response = self.process_advanced_email_command(message)
                print(f"[{self.node_id}] Response: {response}")
                return

        # Record message in conversation history
        self.conversation_history.append({"role": "user", "content": f"{sender_id} says: {message}"})
        if sender_id == "cli_user":
            # Query the LLM using conversation history and log both user and assistant messages
            response = self.query_llm(self.conversation_history)
            self.conversation_history.append({"role": "assistant", "content": response})
            print(f"[{self.node_id}] Response: {response}")

    def _detect_calendar_intent(self, message):
        """
        Detect if the incoming message is related to calendar commands.
        
        The method constructs a prompt asking the LLM to analyze if the message is calendar related,
        and what action is intended (e.g., scheduling, cancellation).
        
        Args:
            message (str): The message to analyze.
        
        Returns:
            dict: A JSON object that includes:
                  - is_calendar_command (bool)
                  - action (string: "schedule_meeting", "cancel_meeting", "list_meetings", "reschedule_meeting", or None)
                  - missing_info (list of strings indicating any missing information)
        """
        
        prompt = f"""
        Analyze this message and determine if it's a calendar-related command: '{message}'
        Return JSON with:
        - is_calendar_command: boolean
        - action: string ("schedule_meeting", "cancel_meeting", "list_meetings", "reschedule_meeting", or null)
        - missing_info: array of strings (what information is missing: "time", "participants", "date", "title")
        """
        
        try:
            response = self.client.chat.completions.create(
                model="gpt-4.1",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f"[{self.node_id}] Error detecting intent: {str(e)}")
            return {"is_calendar_command": False, "action": None, "missing_info": []}

    def _start_meeting_creation(self, initial_message, missing_info):
        """
        Initiate the meeting creation process by setting up a meeting context.
        
        This context holds the initial message and a list of missing pieces of information.
        The process will prompt the user for the missing details.
        
        Args:
            initial_message (str): The original message initiating the meeting creation.
            missing_info (list): List of strings indicating which details are missing.
        """
        
        # Initialize a dictionary to track meeting creation progress
        self.meeting_context = {
            'active': True,
            'initial_message': initial_message,
            'missing_info': missing_info.copy(),
            'collected_info': {}
        }
        
        # Ask for the first missing piece of information
        self._ask_for_next_meeting_info()

    def _ask_for_next_meeting_info(self):
        """
        Ask the user for the next piece of required meeting information.
        
        If all information has been collected, the method proceeds to construct the complete meeting message.
        Otherwise, it selects the next item from the missing_info list and prints a tailored question.
        """
        
        if not self.meeting_context['missing_info']:
            # All required info collected; create complete message and process meeting creation
            combined_message = self._construct_complete_meeting_message()
            self._handle_meeting_creation(combined_message)
            self.meeting_context['active'] = False
            return

        # Get the next missing information item
        next_info = self.meeting_context['missing_info'][0]
        
        # Predefined questions for standard meeting details
        questions = {
            'time': "What time should the meeting be scheduled? (Please use HH:MM format in 24-hour time, e.g., 14:30)",
            'date': "On what date should the meeting be scheduled? (Please use YYYY-MM-DD format, e.g., 2023-12-31)",
            'participants': "Who should attend the meeting? Please list all participants.",
            'title': "What is the title or topic of the meeting?"
        }
        
        # Optionally add context for rescheduling or validation
        context = ""
        if self.meeting_context.get('is_rescheduling', False):
            context = " for rescheduling"
        elif next_info in ['date', 'time'] and 'date' in self.meeting_context['missing_info'] and 'time' in self.meeting_context['missing_info']:
            context = " (please ensure it's a future date and time)"
        
        response = questions.get(next_info, f"Please provide the {next_info} for the meeting") + context
        print(f"[{self.node_id}] Response: {response}")

    def _continue_meeting_creation(self, message, sender_id):
        """
        Continue the meeting creation flow by processing the user's answer.
        
        The response is recorded for the current missing information item, and if additional info is needed,
        the next prompt is issued. Otherwise, the complete meeting creation is triggered.
        
        Args:
            message (str): The user's response for the current information query.
            sender_id (str): The identifier for the sender.
        """        
        
        if not self.meeting_context['missing_info']:
            # Shouldn't happen, but just in case
            self.meeting_context['active'] = False
            return

        # Remove the first missing detail, and save the user's answer under that key
        current_info = self.meeting_context['missing_info'].pop(0)
        self.meeting_context['collected_info'][current_info] = message
        
        if self.meeting_context['missing_info']:
            # More details are still required; ask the next question
            self._ask_for_next_meeting_info()
        else:
            # All information collected: if rescheduling, call the respective handler; otherwise, proceed normally
            if self.meeting_context.get('is_rescheduling', False) and 'target_event_id' in self.meeting_context:
                self._complete_meeting_rescheduling()
            else:
                combined_message = self._construct_complete_meeting_message()
                self._handle_meeting_creation(combined_message)
            
            self.meeting_context['active'] = False
            print(f"[{self.node_id}] Response: Meeting {'rescheduled' if self.meeting_context.get('is_rescheduling') else 'scheduled'} successfully with all required information.")

    def _construct_complete_meeting_message(self):
        """
        Construct a complete meeting instruction message by combining the initial command with the collected details.
        
        Returns:
            str: A complete message string including title, date, time, and participants.
        """
        
        initial = self.meeting_context['initial_message']
        collected = self.meeting_context['collected_info']
        
        # Concatenate all gathered meeting details with appropriate labels
        complete_message = f"{initial} "
        if 'title' in collected:
            complete_message += f"Title: {collected['title']}. "
        if 'date' in collected:
            complete_message += f"Date: {collected['date']}. "
        if 'time' in collected:
            complete_message += f"Time: {collected['time']}. "
        if 'participants' in collected:
            complete_message += f"Participants: {collected['participants']}."
        
        return complete_message

    def _handle_meeting_creation(self, message):
        """
        Handle the complete meeting creation process.
        
        This method extracts meeting details from the combined message, validates them (including checking
        date/time formats and future scheduling), and then attempts to schedule the meeting with Google Calendar.
        
        Args:
            message (str): The complete meeting instruction that includes all necessary details.
        """
        
        # Extract meeting details using an LLM-assisted helper method
        meeting_data = self._extract_meeting_details(message)
        
        # Validate that required fields such as title and participants are present
        required_fields = ['title', 'participants']
        missing = [field for field in required_fields if not meeting_data.get(field)]
        
        if missing:
            print(f"[{self.node_id}] Cannot schedule meeting: missing {', '.join(missing)}")
            return
        
        # Process and normalize participant names
        participants = []
        for p in meeting_data.get("participants", []):
            p_lower = p.lower().strip()
            if p_lower in ["ceo", "marketing", "engineering", "design"]:
                participants.append(p_lower)
        
        # Ensure the current node is included among the participants
        if not participants:
            print(f"[{self.node_id}] Cannot schedule meeting: no valid participants")
            return
            
        # Add the current node if not already included
        if self.node_id not in participants:
            participants.append(self.node_id)
        
        # Process meeting date and time: use provided values or defaults
        meeting_date = meeting_data.get("date", datetime.now().strftime("%Y-%m-%d"))
        meeting_time = meeting_data.get("time", (datetime.now() + timedelta(hours=1)).strftime("%H:%M"))
        
        try:
            # Validate date and time by attempting to parse them
            try:
                start_datetime = datetime.strptime(f"{meeting_date} {meeting_time}", "%Y-%m-%d %H:%M")
                # Check if date is in the past
                current_time = datetime.now()
                if start_datetime < current_time:
                    # Instead of automatically adjusting, ask the user for a valid time
                    print(f"[{self.node_id}] Response: The meeting time {meeting_date} at {meeting_time} is in the past. Please provide a future date and time.")
                    
                    # Store context for follow-up
                    self.meeting_context = {
                        'active': True,
                        'collected_info': {
                            'title': meeting_data.get("title"),
                            'participants': meeting_data.get("participants", [])
                        },
                        'missing_info': ['date', 'time'],
                        'is_rescheduling': False
                    }
                    
                    # Ask for new date and time
                    self._ask_for_next_meeting_info()
                    return
                
            except ValueError:
                # If date parsing fails, notify user instead of auto-fixing
                print(f"[{self.node_id}] Response: I couldn't understand the date/time format. Please provide the date in YYYY-MM-DD format and time in HH:MM format.")
                # Store context for follow-up
                self.meeting_context = {
                    'active': True,
                    'collected_info': {
                        'title': meeting_data.get("title"),
                        'participants': meeting_data.get("participants", [])
                    },
                    'missing_info': ['date', 'time'],
                    'is_rescheduling': False
                }
                
                # Ask for new date and time
                self._ask_for_next_meeting_info()
                return

            # Determine meeting duration (defaulting to 60 minutes if unspecified)
            duration_mins = int(meeting_data.get("duration", 60))
            end_datetime = start_datetime + timedelta(minutes=duration_mins)
            
            # Generate a unique meeting ID and set a meeting title
            meeting_id = f"meeting_{int(datetime.now().timestamp())}"
            meeting_title = meeting_data.get("title", f"Meeting scheduled by {self.node_id}")
            
            # Schedule the meeting using the helper for creating calendar events
            self._create_calendar_meeting(meeting_id, meeting_title, participants, start_datetime, end_datetime)
            
            # Confirm to user with reliable times
            print(f"[{self.node_id}] Meeting '{meeting_title}' scheduled for {meeting_date} at {meeting_time} with {', '.join(participants)}")
        except Exception as e:
            print(f"[{self.node_id}] Error creating meeting: {str(e)}")

    def _extract_meeting_details(self, message):
        """
        Extract detailed meeting information from the given message using LLM assistance.
        
        The function sends a prompt to the LLM to parse the meeting details and returns a structured JSON
        with keys like title, participants, date, time, and duration.
        
        Args:
            message (str): The input meeting instruction message.
        
        Returns:
            dict: A dictionary with meeting details. Missing date/time fields are substituted with defaults.
        """
        
        prompt = f"""
        Extract complete meeting details from:'{message}'
        
        Return JSON with:
        - title: meeting title
        - participants: array of participants (use only: ceo, marketing, engineering, design)
        - date: meeting date (YYYY-MM-DD format, leave empty to use current date)
        - time: meeting time (HH:MM format, leave empty to use current time + 1 hour)
        - duration: duration in minutes (default 60)
        
        If any information is missing, leave the field empty (don't guess).
        """
        
        try:
            response = self.client.chat.completions.create(
                model="gpt-4.1",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            
            # Set defaults if date or time are missing
            if not result.get("date"):
                result["date"] = datetime.now().strftime("%Y-%m-%d")
            
            # Use current time + 1 hour if not specified
            if not result.get("time"):
                result["time"] = (datetime.now() + timedelta(hours=1)).strftime("%H:%M")
            
            return result
        except Exception as e:
            print(f"[{self.node_id}] Error extracting meeting details: {str(e)}")
            return {}

    def _handle_list_meetings(self):
        """
        List upcoming meetings either from the Google Calendar service or the local calendar.
        
        This method retrieves events, formats their details (including title, date/time, and attendees),
        and prints them in a user-friendly format.
        """
        
        if not self.calendar_service:
            print(f"[{self.node_id}] Calendar service not available, showing local meetings only")
            if not self.calendar:
                print(f"[{self.node_id}] No meetings scheduled.")
                return
            
        try:
            # Retrieve current time in the required ISO format for querying events
            now = datetime.utcnow().isoformat() + 'Z'
            events_result = self.calendar_service.events().list(
                calendarId='primary',
                timeMin=now,
                maxResults=10,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])
            
            if not events:
                print(f"[{self.node_id}] No upcoming meetings found.")
                return
            
            print(f"[{self.node_id}] Upcoming meetings:")
            for event in events:
                # Get start time from event details
                start = event['start'].get('dateTime', event['start'].get('date'))
                start_time = datetime.fromisoformat(start.replace('Z', '+00:00'))
                # Format attendee emails by extracting the user part
                attendees = ", ".join([a.get('email', '').split('@')[0] for a in event.get('attendees', [])])
                print(f"  - {event['summary']} on {start_time.strftime('%Y-%m-%d at %H:%M')} with {attendees}")
            
        except Exception as e:
            print(f"[{self.node_id}] Error listing meetings: {str(e)}")

    def _handle_meeting_rescheduling(self, message):
        """
        Handle meeting rescheduling requests by extracting new scheduling details and updating the event.
        
        The method performs the following:
          - Uses LLM to extract rescheduling details such as meeting identifier, original date, new date/time, and duration.
          - Searches the Google Calendar for the meeting to be rescheduled using a simple scoring system.
          - Validates the new date and time.
          - Updates the event in Google Calendar and notifies participants.
        
        Args:
            message (str): The message containing rescheduling instructions.
        """
        
        if not self.calendar_service:
            print(f"[{self.node_id}] Calendar service not available, can't reschedule meetings")
            return
        
        try:
            # Construct a prompt instructing the LLM to extract detailed rescheduling data
            prompt = f"""
            Extract meeting rescheduling details from this message: '{message}'
            
            Identify EXACTLY which meeting needs rescheduling by looking for:
            1. Meeting title or topic (as a simple text string)
            2. Participants involved (as names only)
            3. Original date/time
            
            And what the new schedule should be:
            1. New date (YYYY-MM-DD format)
            2. New time (HH:MM format in 24-hour time)
            3. New duration in minutes (as a number only)
            
            Return a JSON object with these fields:
            - meeting_identifier: A simple text string to identify which meeting to reschedule
            - original_date: Original meeting date if mentioned (YYYY-MM-DD format or null)
            - new_date: New meeting date (YYYY-MM-DD format)
            - new_time: New meeting time (HH:MM format)
            - new_duration: New duration in minutes (or null to keep the same)
            
            IMPORTANT: ALL values must be simple strings or integers, not objects or arrays.
            The meeting_identifier MUST be a simple string.
            """
            
            response = self.client.chat.completions.create(
                model="gpt-4.1",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            
            response_content = response.choices[0].message.content
            try:
                reschedule_data = json.loads(response_content)
            except json.JSONDecodeError as e:
                print(f"[{self.node_id}] Error parsing rescheduling JSON: {e}")
                return
            
            # Extract and normalize data from the JSON response
            meeting_identifier = ""
            if "meeting_identifier" in reschedule_data:
                if isinstance(reschedule_data["meeting_identifier"], str):
                    meeting_identifier = reschedule_data["meeting_identifier"].lower()
                else:
                    meeting_identifier = str(reschedule_data["meeting_identifier"]).lower()

            original_date = None
            if "original_date" in reschedule_data and reschedule_data["original_date"]:
                original_date = str(reschedule_data["original_date"])
            
            new_date = None
            if "new_date" in reschedule_data and reschedule_data["new_date"]:
                new_date = str(reschedule_data["new_date"])
            
            new_time = "10:00"  # Default time
            if "new_time" in reschedule_data and reschedule_data["new_time"]:
                new_time = str(reschedule_data["new_time"])
            
            new_duration = None
            if "new_duration" in reschedule_data and reschedule_data["new_duration"]:
                try:
                    new_duration = int(reschedule_data["new_duration"])
                except (ValueError, TypeError):
                    new_duration = None
            
            # Validate that a meeting identifier and new date are provided
            if not meeting_identifier:
                print(f"[{self.node_id}] Could not determine which meeting to reschedule")
                return
            
            if not new_date:
                print(f"[{self.node_id}] No new date specified for rescheduling")
                return
            
            # Retrieve upcoming meetings to search for a matching event
            try:
                now = datetime.utcnow().isoformat() + 'Z'
                events_result = self.calendar_service.events().list(
                    calendarId='primary',
                    timeMin=now,
                    maxResults=20,
                    singleEvents=True,
                    orderBy='startTime'
                ).execute()
                events = events_result.get('items', [])
            except Exception as e:
                print(f"[{self.node_id}] Error fetching calendar events: {str(e)}")
                return
            
            if not events:
                print(f"[{self.node_id}] No upcoming meetings found to reschedule")
                return
            
            # Use a scoring system to find the best matching event based on title, attendees, and original date
            target_event = None
            best_match_score = 0
            
            for event in events:
                score = 0
                
                # Check title match
                event_title = event.get('summary', '').lower()
                if meeting_identifier in event_title:
                    score += 3
                elif any(word in event_title for word in meeting_identifier.split()):
                    score += 1
                
                # Check attendees match
                attendees = []
                for attendee in event.get('attendees', []):
                    email = attendee.get('email', '')
                    if isinstance(email, str):
                        attendees.append(email.lower())
                    else:
                        attendees.append(str(email).lower())
                    
                if any(meeting_identifier in attendee for attendee in attendees):
                    score += 2
                
                # Check date match if original date was specified
                if original_date:
                    start_time = event['start'].get('dateTime', event['start'].get('date', ''))
                    if isinstance(start_time, str) and original_date in start_time:
                        score += 4
                
                # Update best match if this is better
                if score > best_match_score:
                    best_match_score = score
                    target_event = event
            
            # Require a minimum matching score
            if best_match_score < 1:
                print(f"[{self.node_id}] Could not find a meeting matching '{meeting_identifier}'")
                return
            
            if not target_event:
                print(f"[{self.node_id}] No matching meeting found for '{meeting_identifier}'")
                return
            
            # Validate the new date and time format and ensure the new time is in the future
            try:
                # Parse new date and time
                new_start_datetime = datetime.strptime(f"{new_date} {new_time}", "%Y-%m-%d %H:%M")
                
                # Check if date is in the past
                if new_start_datetime < datetime.now():
                    print(f"[{self.node_id}] Response: The rescheduled time {new_date} at {new_time} is in the past. Please provide a future date and time.")
                    
                    # Ask for new date and time
                    self.meeting_context = {
                        'active': True,
                        'collected_info': {
                            'title': target_event.get('summary', 'Meeting'),  # Keep original title
                            'participants': []  # We'll keep the same participants
                        },
                        'missing_info': ['date', 'time'],
                        'is_rescheduling': True,
                        'target_event_id': target_event['id'],
                        'target_event': target_event  # Store the whole event to preserve details
                    }
                    
                    self._ask_for_next_meeting_info()
                    return
            except ValueError:
                print(f"[{self.node_id}] Response: I couldn't understand the date/time format. Please provide the date in YYYY-MM-DD format and time in HH:MM format.")
                
                # Ask for new date and time
                self.meeting_context = {
                    'active': True,
                    'collected_info': {
                        'title': target_event.get('summary', 'Meeting'),  # Keep original title
                        'participants': []  # We'll keep the same participants
                    },
                    'missing_info': ['date', 'time'],
                    'is_rescheduling': True,
                    'target_event_id': target_event['id'],
                    'target_event': target_event  # Store the whole event to preserve details
                }
                
                self._ask_for_next_meeting_info()
                return
            
            # Determine the new end time using either the provided new duration or the event's original duration
            try:
                # Extract original start and end times
                original_start = datetime.fromisoformat(target_event['start'].get('dateTime').replace('Z', '+00:00'))
                original_end = datetime.fromisoformat(target_event['end'].get('dateTime').replace('Z', '+00:00'))
                original_duration = (original_end - original_start).total_seconds() / 60
                
                # Use new duration if specified, otherwise keep original duration
                if new_duration is not None and new_duration > 0:
                    duration_to_use = new_duration
                else:
                    duration_to_use = original_duration
                    
                new_end_datetime = new_start_datetime + timedelta(minutes=duration_to_use)
                
                # Update the target event's start and end times
                target_event['start']['dateTime'] = new_start_datetime.isoformat()
                target_event['end']['dateTime'] = new_end_datetime.isoformat()
                
                # Update event in Google Calendar
                updated_event = self.calendar_service.events().update(
                    calendarId='primary',
                    eventId=target_event['id'],
                    body=target_event
                ).execute()
                
                # Print success message with user-friendly time format
                meeting_title = updated_event.get('summary', 'Untitled meeting')
                formatted_time = new_start_datetime.strftime("%I:%M %p")  # 12-hour format with AM/PM
                formatted_date = new_start_datetime.strftime("%B %d, %Y")  # Month day, year
                
                print(f"[{self.node_id}] Response: Meeting '{meeting_title}' has been rescheduled to {formatted_date} at {formatted_time}.")
                
                # Update local calendar records
                for meeting in self.calendar:
                    if meeting.get('event_id') == updated_event['id']:
                        meeting['meeting_info'] = f"{meeting_title} (Rescheduled to {new_date} at {formatted_time})"
                
                # Notify all attendees about the rescheduled meeting
                attendees = updated_event.get('attendees', [])
                for attendee in attendees:
                    attendee_id = attendee.get('email', '').split('@')[0]
                    if attendee_id in self.network.nodes:
                        # Update their local calendar
                        for meeting in self.network.nodes[attendee_id].calendar:
                            if meeting.get('event_id') == updated_event['id']:
                                meeting['meeting_info'] = f"{meeting_title} (Rescheduled to {new_date} at {formatted_time})"
                        
                        # Send notifications
                        notification = (
                            f"Your meeting '{meeting_title}' has been rescheduled by {self.node_id}.\n"
                            f"New date: {formatted_date}\n"
                            f"New time: {formatted_time}\n"
                            f"Duration: {int(duration_to_use)} minutes"
                        )
                        self.network.send_message(self.node_id, attendee_id, notification)
                
            except Exception as e:
                print(f"[{self.node_id}] Error updating the meeting: {str(e)}")
                print(f"[{self.node_id}] Response: There was an error rescheduling the meeting. Please try again.")
            
        except Exception as e:
            print(f"[{self.node_id}] General error in meeting rescheduling: {str(e)}")

    def send_message(self, recipient_id: str, content: str):
        """
        Send a message from this node to another node via the network.
        
        If the recipient is 'cli_user', print the message directly.
        Otherwise, use the network's send_message method.
        
        Args:
            recipient_id (str): The target node identifier.
            content (str): The content of the message.
        """
        
        if not self.network:
            print(f"[{self.node_id}] No network attached.")
            return
        
        # Directly print messages to CLI user
        if recipient_id == "cli_user":
            print(f"[{self.node_id}] Response: {content}")
        else:
            self.network.send_message(self.node_id, recipient_id, content)

    def query_llm(self, messages):
        """
        Query the language model with a list of messages.
        
        A system prompt is prepended to guide the LLM to be short and concise.
        
        Args:
            messages (list): A list of message dictionaries (role and content).
        
        Returns:
            str: The trimmed text response from the LLM.
        """
        
        system_prompt = [{
            "role": "system",
            "content": (
                "You are a direct and concise AI agent for an organization. "
                "Provide short, to-the-point answers and do not continue repeating Goodbyes. "
                "End after conveying necessary information."
            )
        }]

        combined_messages = system_prompt + messages
        try:
            # Log the API request
            log_api_request("openai_chat", {"model": self.llm_params["model"], "messages": combined_messages})
            
            completion = self.client.chat.completions.create(
                model=self.llm_params["model"],
                messages=combined_messages,
                temperature=self.llm_params["temperature"],
                max_tokens=self.llm_params["max_tokens"]
            )
            
            response_content = completion.choices[0].message.content.strip()
            
            # Log the API response
            log_api_response("openai_chat", {"response": response_content})
            
            # Log the agent's response
            log_agent_message(self.node_id, response_content)
            
            return response_content
        except Exception as e:
            error_msg = f"LLM query failed: {e}"
            print(f"[{self.node_id}] {error_msg}")
            log_error(error_msg)
            return "LLM query failed."

    def plan_project(self, project_id: str, objective: str):
        """
        Create a detailed project plan using the LLM.
        
        This method sends the project objective to the LLM to generate a plan in JSON format, parses
        the resulting plan for stakeholders and steps, writes the plan to a file, and schedules a meeting.
        
        Args:
            project_id (str): The identifier for the project.
            objective (str): The objective or goal of the project.
        """
        
        if project_id not in self.projects:
            self.projects[project_id] = {
                "name": objective,
                "plan": [],
                "participants": set()
            }

        plan_prompt = f"""
        You are creating a detailed project plan for project '{project_id}'.
        Objective: {objective}

        The plan should include:
        1. All stakeholders involved in the project. Use only these roles: CEO, Marketing, Engineering, Design.
        2. Detailed steps needed to execute the plan, including time and cost estimates.
        Each step should be written in paragraphs and full sentences.

        Return valid JSON only, with this structure:
        {{
          "stakeholders": ["list of stakeholders"],
          "steps": [
            {{
              "description": "Detailed step description with time and cost estimates"
            }}
          ]
        }}
        Keep it concise. End after providing the JSON. No extra words.
        """

        response = self.query_llm([{"role": "user", "content": plan_prompt}])
        print(f"[{self.node_id}] LLM raw response (project '{project_id}'): {response}")

        # --- Start: Extract JSON from potential markdown fences ---
        json_to_parse = response.strip()
        match = re.search(r"```json\n(.+)\n```", json_to_parse, re.DOTALL | re.IGNORECASE)
        if match:
            json_to_parse = match.group(1).strip()
        else:
            # If the response appears to be plain JSON without fences, use it as is.
            if json_to_parse.startswith("{") and json_to_parse.endswith("}"):
                pass # Assume it's already JSON
            else:
                # If no fences and doesn't look like JSON, it's likely an error message
                print(f"[{self.node_id}] LLM response doesn't appear to be JSON: {json_to_parse}")
                print(f"[{self.node_id}] Response: Could not generate project plan. The AI's response was not in the expected format.")
                return
        # --- End: Extract JSON ---

        try:
            # Attempt to parse the extracted JSON response
            data = json.loads(json_to_parse) 
            stakeholders = data.get("stakeholders", [])
            steps = data.get("steps", [])
            self.projects[project_id]["plan"] = steps

            # --- Start: Format and print plan details for UI response ---
            plan_summary = f"Project '{project_id}' plan created:\n"
            plan_summary += f"Stakeholders: {', '.join(stakeholders)}\n"
            plan_summary += "Steps:\n"
            for i, step in enumerate(steps, 1):
                plan_summary += f"  {i}. {step.get('description', 'No description')}\n"
            # Print the summary which will be captured as the response
            print(f"[{self.node_id}] Response: {plan_summary.strip()}")
            # --- End: Format and print plan details ---

            # Save the project plan to a text file
            with open(f"{project_id}_plan.txt", "w", encoding="utf-8") as file:
                file.write(f"Project ID: {project_id}\\n")
                file.write(f"Objective: {objective}\\n")
                file.write("Stakeholders:\\n")
                for stakeholder in stakeholders:
                    file.write(f"  - {stakeholder}\\n")
                file.write("Steps:\\n")
                for step in steps:
                    file.write(f"  - {step.get('description', '')}\\n")

            # Map stakeholder roles to node identifiers, case-insensitively
            role_to_node = {
                "ceo": "ceo",
                "marketing": "marketing",
                "engineering": "engineering",
                "design": "design"
            }

            participants = []
            for stakeholder in stakeholders:
                # Normalize the role name (lowercase and remove extra spaces)
                role = stakeholder.lower().strip()
                
                # Check for partial matches
                matched = False
                for key in role_to_node:
                    if key in role:
                        node_id = role_to_node[key]
                        participants.append(node_id)
                        self.projects[project_id]["participants"].add(node_id)
                        matched = True
                        break
                
                if not matched:
                    print(f"[{self.node_id}] No mapping for stakeholder '{stakeholder}'. Skipping.")

            print(f"[{self.node_id}] Project participants: {participants}")
            
            # Schedule a meeting if valid participants were identified
            if participants:
                self.schedule_meeting(project_id, participants)
            else:
                print(f"[{self.node_id}] No valid participants identified for project '{project_id}'. Skipping meeting schedule.")
            
            # Generate tasks based on the plan
            self.generate_tasks_from_plan(project_id, steps, participants)

            # Emit update events (assuming a global socketio object)
            print(f"[{self.node_id}] Emitting update events for UI.")
            # Make sure socketio is accessible here. Assuming it's global for simplicity.
            socketio.emit('update_projects') 
            socketio.emit('update_tasks')
            
        except json.JSONDecodeError as e:
            # Handle JSON parsing failure
            print(f"[{self.node_id}] Failed to parse JSON plan: {e}")
            print(f"[{self.node_id}] Received non-JSON response from LLM: {response}")
            # Inform the user via the response mechanism
            print(f"[{self.node_id}] Response: Could not generate project plan. The AI's response was not in the expected format.")
            return # Stop processing the plan if JSON is invalid

    def generate_tasks_from_plan(self, project_id: str, steps: list, participants: list):
        """
        Generate tasks from a project plan by creating task objects using LLM-assisted function calling.
        
        For each step in the plan, this method constructs a prompt to generate 1-3 tasks, calls the LLM with a
        function tool specification (create_task), parses the returned task details, and creates the Task objects.
        
        Args:
            project_id (str): Identifier for the project.
            steps (list): List of steps from the project plan.
            participants (list): List of node identifiers who are the project participants.
        """
        
        # Define the function for task creation
        functions = [
            {
                "type": "function",
                "function": {
                    "name": "create_task",
                    "description": "Create a task from a project step",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Short title for the task"
                            },
                            "description": {
                                "type": "string",
                                "description": "Detailed description of what needs to be done"
                            },
                            "assigned_to": {
                                "type": "string",
                                "description": "Role responsible for this task (marketing, engineering, design, ceo)"
                            },
                            "due_date_offset": {
                                "type": "integer",
                                "description": "Days from now when the task is due"
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": "Priority level of the task"
                            }
                        },
                        "required": ["title", "description", "assigned_to", "due_date_offset", "priority"]
                    }
                }
            }
        ]
        
        # Process each project plan step
        for i, step in enumerate(steps):
            step_description = step.get("description", "")
            
            prompt = f"""
            For project '{project_id}', analyze this step and create appropriate tasks:
            
            Step: {step_description}
            
            Available roles: {', '.join(participants)}
            
            Create 1-3 specific tasks from this step. Each task should be assigned to the most appropriate role.
            """
            
            try:
                response = self.client.chat.completions.create(
                    model="gpt-4.1",
                    messages=[{"role": "user", "content": prompt}],
                    tools=functions,
                    tool_choice={"type": "function", "function": {"name": "create_task"}}
                )
                
                # Process any function calls in the response to create tasks
                for choice in response.choices:
                    if hasattr(choice.message, 'tool_calls') and choice.message.tool_calls:
                        for tool_call in choice.message.tool_calls:
                            if tool_call.function.name == "create_task":
                                task_data = json.loads(tool_call.function.arguments)
                                
                                # Create a new Task using the provided data
                                due_date = datetime.now() + timedelta(days=task_data["due_date_offset"])
                                task = Task(
                                    title=task_data["title"],
                                    description=task_data["description"],
                                    due_date=due_date,
                                    assigned_to=task_data["assigned_to"],
                                    priority=task_data["priority"],
                                    project_id=project_id
                                )
                                
                                # Add to network tasks
                                if self.network:
                                    self.network.add_task(task)
                                    print(f"[{self.node_id}] Created task: {task}")
                                    
                                    # Create a calendar reminder for the task
                                    self.create_calendar_reminder(task)
            
            except Exception as e:
                print(f"[{self.node_id}] Error generating tasks for step {i+1}: {e}")

    def list_tasks(self):
        """
        List all tasks assigned to this node.
        
        Retrieves tasks for this node from the network and formats a string summary.
        
        Returns:
            str: A formatted string of tasks with their titles, due dates, priority, and descriptions.
        """
        
        if not self.network:
            return "No network connected."
            
        tasks = self.network.get_tasks_for_node(self.node_id)
        if not tasks:
            return f"No tasks assigned to {self.node_id}."
            
        result = f"Tasks for {self.node_id}:\n"
        for i, task in enumerate(tasks, 1):
            result += f"{i}. {task.title} (Due: {task.due_date.strftime('%Y-%m-%d')}, Priority: {task.priority})\n"
            result += f"   Description: {task.description}\n"
            
        return result

    def _handle_meeting_cancellation(self, message):
        """
        Handle meeting cancellation requests based on natural language commands.
        
        This method:
          - Uses LLM to extract cancellation details from the message.
          - Retrieves upcoming meetings.
          - Filters meetings based on specified title, participants, and date criteria.
          - Deletes matching events from Google Calendar and notifies participants.
        
        Args:
            message (str): The cancellation command as a natural language message.
        """
        
        # First, get all meetings from calendar
        if not self.calendar_service:
            print(f"[{self.node_id}] Calendar service not available, can't cancel meetings")
            return
        
        try:
            # Use OpenAI to extract cancellation details
            prompt = f"""
            Extract meeting cancellation details from this message: '{message}'
            
            Return a JSON object with these fields:
            - title: The meeting title or topic to cancel (or null if not specified)
            - with_participants: Array of participants in the meeting to cancel (or empty if not specified)
            - date: Meeting date to cancel (YYYY-MM-DD format, or null if not specified)
            
            Only include information that is explicitly mentioned.
            """
            
            response = self.client.chat.completions.create(
                model="gpt-4.1",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            
            cancel_data = json.loads(response.choices[0].message.content)
            
            # Get upcoming meetings
            now = datetime.utcnow().isoformat() + 'Z'
            events_result = self.calendar_service.events().list(
                calendarId='primary',
                timeMin=now,
                maxResults=10,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])
            
            if not events:
                print(f"[{self.node_id}] No upcoming meetings found to cancel")
                return
            
            # Filter events based on cancellation criteria
            title_filter = cancel_data.get("title")
            participants_filter = [p.lower() for p in cancel_data.get("with_participants", [])]
            date_filter = cancel_data.get("date")
            
            cancelled_count = 0

            # Iterate over events and determine if they match the cancellation criteria
            for event in events:
                should_cancel = True
                
                # Check title match if specified
                if title_filter and title_filter.lower() not in event.get('summary', '').lower():
                    should_cancel = False
                
                # Check participants if specified
                if participants_filter:
                    event_attendees = [a.get('email', '').split('@')[0].lower() 
                                      for a in event.get('attendees', [])]
                    if not any(p in event_attendees for p in participants_filter):
                        should_cancel = False
                
                # Check date if specified
                if date_filter:
                    event_start = event.get('start', {}).get('dateTime', event.get('start', {}).get('date'))
                    if event_start and date_filter not in event_start:
                        should_cancel = False
                
                if should_cancel:
                    # Delete the event from the calendar
                    self.calendar_service.events().delete(
                        calendarId='primary',
                        eventId=event['id']
                    ).execute()
                    
                    # Remove the event from the local calendar records
                    self.calendar = [m for m in self.calendar if m.get('event_id') != event['id']]
                    
                    # Notify attendees about the cancellation
                    event_attendees = [a.get('email', '').split('@')[0] for a in event.get('attendees', [])]
                    for attendee in event_attendees:
                        if attendee in self.network.nodes:
                            # Update their local calendar
                            self.network.nodes[attendee].calendar = [
                                m for m in self.network.nodes[attendee].calendar 
                                if m.get('event_id') != event['id']
                            ]
                            # Notify them
                            notification = f"Meeting '{event.get('summary')}' has been cancelled by {self.node_id}"
                            self.network.send_message(self.node_id, attendee, notification)
                
                    cancelled_count += 1
                    print(f"[{self.node_id}] Cancelled meeting: {event.get('summary')}")
            
            if cancelled_count == 0:
                print(f"[{self.node_id}] No meetings found matching the cancellation criteria")
            else:
                print(f"[{self.node_id}] Cancelled {cancelled_count} meeting(s)")
            
        except Exception as e:
            print(f"[{self.node_id}] Error cancelling meeting: {str(e)}")

    def _create_calendar_meeting(self, meeting_id, title, participants, start_datetime, end_datetime):
        """
        Create a meeting event in Google Calendar.
        
        Constructs the event details, attempts to insert the event into the primary calendar,
        updates the local calendar records, and sends notifications to other participants.
        If the calendar service is unavailable, falls back to local scheduling.
        
        Args:
            meeting_id (str): Unique identifier for the meeting.
            title (str): The title or summary for the meeting.
            participants (list): List of participant identifiers.
            start_datetime (datetime): The start time of the meeting.
            end_datetime (datetime): The end time of the meeting.
        """
        
        # If calendar service is not available, fall back to local scheduling
        if not self.calendar_service:
            print(f"[{self.node_id}] Calendar service not available, using local scheduling")
            self._fallback_schedule_meeting(meeting_id, participants)
            return
        
        # Create event
        event = {
            'summary': title,
            'start': {
                'dateTime': start_datetime.isoformat(),
                'timeZone': 'UTC',
            },
            'end': {
                'dateTime': end_datetime.isoformat(),
                'timeZone': 'UTC',
            },
            'attendees': [{'email': f'{p}@example.com'} for p in participants],
        }

        try:
            event = self.calendar_service.events().insert(calendarId='primary', body=event).execute()
            
            # Correctly format date and time for user display
            meeting_date = start_datetime.strftime("%Y-%m-%d")
            meeting_time = start_datetime.strftime("%H:%M")
            
            print(f"[{self.node_id}] Meeting created: {event.get('htmlLink')}")
            print(f"[{self.node_id}] Meeting '{title}' scheduled for {meeting_date} at {meeting_time} with {', '.join(participants)}")
            
            # Add the meeting to the local calendar
            self.calendar.append({
                'project_id': meeting_id,
                'meeting_info': title,
                'event_id': event['id']
            })

            # Notify each participant (if not the sender) about the scheduled meeting
            for p in participants:
                if p != self.node_id and p in self.network.nodes:
                    self.network.nodes[p].calendar.append({
                        'project_id': meeting_id,
                        'meeting_info': title,
                        'event_id': event['id']
                    })
                    notification = f"New meeting: '{title}' scheduled by {self.node_id} for {meeting_date} at {meeting_time}"
                    self.network.send_message(self.node_id, p, notification)
        except Exception as e:
            print(f"[{self.node_id}] Failed to create calendar event: {e}")
            # Fallback to local calendar
            self._fallback_schedule_meeting(meeting_id, participants)

    def _complete_meeting_rescheduling(self):
        """
        Complete the meeting rescheduling process using collected meeting context details.
        
        This method retrieves the target event, parses the new date and time, adjusts if the time is in the past,
        updates the event's start and end times, and notifies participants about the change.
        """
        
        if not hasattr(self, 'meeting_context') or not self.meeting_context.get('active'):
            return
        
        # Get the new date and time
        new_date = self.meeting_context['collected_info'].get('date')
        new_time = self.meeting_context['collected_info'].get('time')
        target_event_id = self.meeting_context.get('target_event_id')
        
        try:
            # Get the full event
            event = self.calendar_service.events().get(
                calendarId='primary',
                eventId=target_event_id
            ).execute()
            
            # Parse the new date and time
            new_start_datetime = datetime.strptime(f"{new_date} {new_time}", "%Y-%m-%d %H:%M")
            
            # Check if it's still in the past
            if new_start_datetime < datetime.now():
                print(f"[{self.node_id}] The provided time is still in the past. Adjusting to tomorrow at the same time.")
                tomorrow = datetime.now() + timedelta(days=1)
                new_start_datetime = datetime(
                    tomorrow.year, tomorrow.month, tomorrow.day,
                    new_start_datetime.hour, new_start_datetime.minute
                )
            
            # Calculate end time based on original duration
            original_start = datetime.fromisoformat(event['start'].get('dateTime').replace('Z', '+00:00'))
            original_end = datetime.fromisoformat(event['end'].get('dateTime').replace('Z', '+00:00'))
            original_duration = (original_end - original_start).total_seconds() / 60
            
            new_end_datetime = new_start_datetime + timedelta(minutes=original_duration)
            
            # Update the event times while preserving all other data
            event['start']['dateTime'] = new_start_datetime.isoformat()
            event['end']['dateTime'] = new_end_datetime.isoformat()
            
            # Update event in Google Calendar
            updated_event = self.calendar_service.events().update(
                calendarId='primary',
                eventId=target_event_id,
                body=event
            ).execute()
            
            # Format date and time for user-friendly display
            meeting_title = updated_event.get('summary', 'Untitled meeting')
            formatted_time = new_start_datetime.strftime("%I:%M %p")
            formatted_date = new_start_datetime.strftime("%B %d, %Y")
            
            # Success message
            print(f"[{self.node_id}] Response: Meeting '{meeting_title}' has been rescheduled to {formatted_date} at {formatted_time}.")
            
            # Update local calendar records and notify participants
            for meeting in self.calendar:
                if meeting.get('event_id') == updated_event['id']:
                    meeting['meeting_info'] = f"{meeting_title} (Rescheduled to {formatted_date} at {formatted_time})"
            
            # Notify each attendee about the updated meeting details
            attendees = updated_event.get('attendees', [])
            for attendee in attendees:
                attendee_id = attendee.get('email', '').split('@')[0]
                if attendee_id in self.network.nodes:
                    # Update their local calendar
                    for meeting in self.network.nodes[attendee_id].calendar:
                        if meeting.get('event_id') == updated_event['id']:
                            meeting['meeting_info'] = f"{meeting_title} (Rescheduled to {formatted_date} at {formatted_time})"
                    
                    # Send notification
                    notification = (
                        f"Your meeting '{meeting_title}' has been rescheduled by {self.node_id}.\n"
                        f"New date: {formatted_date}\n"
                        f"New time: {formatted_time}"
                    )
                    self.network.send_message(self.node_id, attendee_id, notification)
        
        except Exception as e:
            print(f"[{self.node_id}] Error completing meeting rescheduling: {str(e)}")
            print(f"[{self.node_id}] Response: There was an error rescheduling the meeting. Please try again.")

    def fetch_emails(self, max_results=10, query=None):
        """
        Fetch emails from the Gmail account using the Gmail service.
        
        Args:
            max_results (int): Maximum number of emails to fetch.
            query (str, optional): A search query to filter the emails.
        
        Returns:
            list: A list of emails with details like subject, sender, date, snippet, and body.
        """
        
        if not self.gmail_service:
            print(f"[{self.node_id}] Gmail service not available")
            return []
        
        try:
            # Default query to get recent emails
            query_string = query if query else ""
            
            # Get list of messages matching the query
            results = self.gmail_service.users().messages().list(
                userId='me',
                q=query_string,
                maxResults=max_results
            ).execute()
            
            messages = results.get('messages', [])
            
            if not messages:
                print(f"[{self.node_id}] No emails found matching query: {query_string}")
                return []
            
            # Fetch full details for each message
            emails = []
            for message in messages:
                msg_id = message['id']
                msg = self.gmail_service.users().messages().get(
                    userId='me', 
                    id=msg_id, 
                    format='full'
                ).execute()
                
                # Extract header information
                headers = msg['payload']['headers']
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '(No subject)')
                sender = next((h['value'] for h in headers if h['name'] == 'From'), '(Unknown sender)')
                date = next((h['value'] for h in headers if h['name'] == 'Date'), '')
                
                # Extract body content
                body = self._extract_email_body(msg['payload'])
                
                # Add email data to list
                emails.append({
                    'id': msg_id,
                    'subject': subject,
                    'sender': sender,
                    'date': date,
                    'body': body,
                    'snippet': msg.get('snippet', ''),
                    'labelIds': msg.get('labelIds', [])
                })
            
            print(f"[{self.node_id}] Fetched {len(emails)} emails")
            return emails
        
        except Exception as e:
            print(f"[{self.node_id}] Error fetching emails: {str(e)}")
            return []
    
    def _extract_email_body(self, payload):
        """
        Recursively extract the email body text from the Gmail message payload.
        
        Handles both single-part and multipart messages by performing base64 decoding.
        
        Args:
            payload (dict): The payload section of a Gmail message.
        
        Returns:
            str: Decoded text content of the email, or a placeholder if not found.
        """
        
        if 'body' in payload and payload['body'].get('data'):
            # Base64 decode the body
            body_data = payload['body']['data']
            body_bytes = base64.urlsafe_b64decode(body_data)
            return body_bytes.decode('utf-8')
        
        # If the payload has parts (multipart email), recursively extract from parts
        if 'parts' in payload:
            text_parts = []
            for part in payload['parts']:
                # Focus on text/plain parts first, fall back to HTML if needed
                if part['mimeType'] == 'text/plain':
                    text_parts.append(self._extract_email_body(part))
                elif part['mimeType'] == 'text/html' and not text_parts:
                    text_parts.append(self._extract_email_body(part))
                elif part['mimeType'].startswith('multipart/'):
                    text_parts.append(self._extract_email_body(part))
            
            return '\n'.join(text_parts)
        
        return "(No content)"
    
    def summarize_emails(self, emails, summary_type="concise"):
        """
        Summarize a list of emails using the LLM.
        
        Constructs a prompt by concatenating email details and requests either a concise or detailed summary.
        
        Args:
            emails (list): List of email dictionaries.
            summary_type (str): "concise" or "detailed" summary preference.
        
        Returns:
            str: The summary produced by the LLM.
        """
        
        if not emails:
            return "No emails to summarize."
        
        # Prepare the email data for the LLM
        email_texts = []
        for i, email in enumerate(emails, 1):
            email_texts.append(
                f"Email {i}:\n"
                f"From: {email['sender']}\n"
                f"Subject: {email['subject']}\n"
                f"Date: {email['date']}\n"
                f"Snippet: {email['snippet']}\n"
            )
        
        emails_content = "\n\n".join(email_texts)
        
        # Choose prompt based on summary type
        if summary_type == "detailed":
            prompt = f"""
            Please provide a detailed summary of the following emails:
            {emails_content}
            
            For each email, include:
            1. The sender
            2. The subject
            3. Key points from the email
            4. Any action items or important deadlines
            """
        else:
            # Default to concise summary
            prompt = f"""
            Please provide a concise summary of the following emails:
            {emails_content}
            
            Keep your summary brief and focus on the most important information.
            """
        
        # Get summary from the LLM
        response = self.query_llm([{"role": "user", "content": prompt}])
        return response
    
    def process_email_command(self, command):
        """
        Process a natural language command related to emails.
        
        Detects the intent (e.g., fetch recent, search) and calls the appropriate email processing method.
        
        Args:
            command (str): The email command in natural language.
        
        Returns:
            str: The result or summary of the email action.
        """
        
        # First, detect the intent of the email command
        intent = self._detect_email_intent(command)
        
        action = intent.get("action")
        
        if action == "fetch_recent":
            # Get recent emails
            count = intent.get("count", 5)
            emails = self.fetch_emails(max_results=count)
            if not emails:
                return "I couldn't find any recent emails."
            
            summary_type = intent.get("summary_type", "concise")
            return self.summarize_emails(emails, summary_type)
            
        elif action == "search":
            # Search emails with query
            query = intent.get("query", "")
            count = intent.get("count", 5)
            
            if not query:
                return "I need a search query to find emails. Please specify what you're looking for."
            
            emails = self.fetch_emails(max_results=count, query=query)
            if not emails:
                return f"I couldn't find any emails matching '{query}'."
            
            summary_type = intent.get("summary_type", "concise")
            return self.summarize_emails(emails, summary_type)
            
        else:
            return "I'm not sure what you want to do with your emails. Try asking for recent emails or searching for specific emails."
    
    def _detect_email_intent(self, message):
        """
        Detect the intent of an email-related command using LLM-based analysis.
        
        Constructs a prompt asking the LLM to output a JSON object with fields indicating:
          - The action ("fetch_recent", "search", or "none")
          - Count (number of emails to fetch)
          - Query (if searching)
          - Summary type ("concise" or "detailed")
        
        Args:
            message (str): The email command to analyze.
        
        Returns:
            dict: Parsed JSON object with detected intent details.
        """
        
        prompt = f"""
        Analyze this message and determine what email action is being requested:
        '{message}'
        
        Return JSON with these fields:
        - action: string ("fetch_recent", "search", "none")
        - count: integer (number of emails to fetch/search, default 5)
        - query: string (search query if applicable)
        - summary_type: string ("concise" or "detailed")
        
        Only extract information explicitly mentioned in the message.
        """
        
        try:
            response = self.client.chat.completions.create(
                model="gpt-4.1",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f"[{self.node_id}] Error detecting email intent: {str(e)}")
            # Default fallback
            return {"action": "none", "count": 5, "query": "", "summary_type": "concise"}

    def fetch_emails_with_advanced_query(self, criteria):
        """
        Fetch emails using advanced filtering criteria.
        
        Builds a Gmail query string from the provided criteria dictionary and calls fetch_emails.
        
        Args:
            criteria (dict): Dictionary with keys like 'from', 'to', 'subject', 'has_attachment',
                             'label', 'is_unread', 'after', 'before', 'keywords', 'max_results'.
        
        Returns:
            list: A list of emails matching the advanced criteria.
        """
        
        if not self.gmail_service:
            return []
            
        # Build Gmail query string from criteria
        query_parts = []
        
        # Add filters for common criteria
        if criteria.get('from'):
            query_parts.append(f"from:{criteria['from']}")
        
        if criteria.get('to'):
            query_parts.append(f"to:{criteria['to']}")
            
        if criteria.get('subject'):
            query_parts.append(f"subject:{criteria['subject']}")
            
        if criteria.get('has_attachment', False):
            query_parts.append("has:attachment")
            
        if criteria.get('label'):
            query_parts.append(f"label:{criteria['label']}")
            
        if criteria.get('is_unread', False):
            query_parts.append("is:unread")
            
        # Handle date ranges
        if criteria.get('after'):
            query_parts.append(f"after:{criteria['after']}")
            
        if criteria.get('before'):
            query_parts.append(f"before:{criteria['before']}")
            
        # Add keywords/content search
        if criteria.get('keywords'):
            if isinstance(criteria['keywords'], list):
                query_parts.append(" ".join(criteria['keywords']))
            else:
                query_parts.append(criteria['keywords'])
        
        # Combine all parts into a single query
        query = " ".join(query_parts)
        max_results = criteria.get('max_results', 10)
        
        print(f"[{self.node_id}] Fetching emails with query: {query}")
        return self.fetch_emails(max_results=max_results, query=query)
    
    def get_email_labels(self):
        """
        Retrieve available email labels from Gmail.
        
        Fetches the labels, formats them in a user-friendly way, and returns them.
        
        Returns:
            list: List of dictionaries with label id, name, and type.
        """        
        
        if not self.gmail_service:
            print(f"[{self.node_id}] Gmail service not available")
            return []
            
        try:
            results = self.gmail_service.users().labels().list(userId='me').execute()
            labels = results.get('labels', [])
            
            # Format labels for user-friendly display
            formatted_labels = []
            for label in labels:
                formatted_labels.append({
                    'id': label['id'],
                    'name': label['name'],
                    'type': label['type']  # 'system' or 'user'
                })
                
            return formatted_labels
            
        except Exception as e:
            print(f"[{self.node_id}] Error fetching email labels: {str(e)}")
            return []
            
    def process_advanced_email_command(self, command):
        """
        Process a complex email command using advanced parsing.
        
        First analyzes the command to extract detailed intent and parameters.
        Depending on the action (e.g., list_labels, advanced_search), it calls appropriate functions.
        
        Args:
            command (str): The advanced email command in natural language.
        
        Returns:
            str: The output or response from processing the advanced email command.
        """
        
        # First analyze the command to extract detailed intent and parameters
        analysis = self._analyze_email_command(command)
        
        action = analysis.get('action', 'none')
        
        if action == 'list_labels':
            # Get and format available labels
            labels = self.get_email_labels()
            if not labels:
                return "I couldn't retrieve your email labels."
                
            # Format response with label categories
            system_labels = [l for l in labels if l['type'] == 'system']
            user_labels = [l for l in labels if l['type'] == 'user']
            
            response = "Here are your email labels:\n\n"
            
            if system_labels:
                response += "System Labels:\n"
                for label in system_labels:
                    response += f"- {label['name']}\n"
            
            if user_labels:
                response += "\nCustom Labels:\n"
                for label in user_labels:
                    response += f"- {label['name']}\n"
                    
            return response
            
        elif action == 'advanced_search':
            # Extract search criteria from analysis
            criteria = analysis.get('criteria', {})
            
            if not criteria:
                return "I couldn't understand your search criteria. Please try again with more specific details."
                
            # Fetch emails matching criteria
            emails = self.fetch_emails_with_advanced_query(criteria)
            
            if not emails:
                return "I couldn't find any emails matching your criteria."
                
            # Summarize emails with requested format
            summary_type = analysis.get('summary_type', 'concise')
            return self.summarize_emails(emails, summary_type)
            
        else:
            # Fall back to basic email processing
            return self.process_email_command(command)
    
    def _analyze_email_command(self, command):
        """
        Analyze a complex email command to extract detailed parameters.
        
        This method sends a prompt to the LLM requesting a JSON output with the structure
        specifying action, criteria, and summary type.
        
        Args:
            command (str): The complex email command.
        
        Returns:
            dict: Parsed JSON with fields "action", "criteria", and "summary_type".
        """
        

        """Analyze a complex email command to extract detailed intent and parameters"""
        # If we're in email composition mode, skip this analysis
        if hasattr(self, 'email_context') and self.email_context.get('active'):
            return {"action": "none"}
            
        prompt = f"""
        Analyze this email-related command in detail:
        '{command}'
        
        Return a JSON object with the following structure:
        {{
            "action": "list_labels" | "advanced_search" | "fetch_recent" | "search" | "none",
            "criteria": {{
                "from": "sender email or name",
                "to": "recipient email",
                "subject": "subject text",
                "keywords": ["word1", "word2"],
                "has_attachment": true/false,
                "is_unread": true/false,
                "label": "label name",
                "after": "YYYY/MM/DD",
                "before": "YYYY/MM/DD",
                "max_results": 10
            }},
            "summary_type": "concise" | "detailed"
        }}
        
        Include only the fields that are explicitly mentioned or clearly implied in the command.
        Convert date references like "yesterday", "last week", "2 days ago" to YYYY/MM/DD format.
        """
        
        try:
            response = self.client.chat.completions.create(
                model="gpt-4.1",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f"[{self.node_id}] Error analyzing email command: {str(e)}")
            return {"action": "none", "criteria": {}, "summary_type": "concise"}

    def send_email(self, to, subject, body):
        """Send an email using Gmail API"""
        if not self.gmail_service:
            error_msg = f"{self.node_id} Gmail service not available, can't send email"
            print(f"[{self.node_id}] {error_msg}")
            log_error(error_msg)
            return False
            
        try:
            # Create the email message
            message = {
                'raw': self._create_message(to, subject, body)
            }
            
            # Log the email sending attempt
            log_system_message(f"Sending email from {self.node_id} to {to} with subject: {subject}")
            
            # Send the email
            sent_message = self.gmail_service.users().messages().send(
                userId='me',
                body=message
            ).execute()
            
            success_msg = f"Email sent successfully with message ID: {sent_message['id']}"
            print(f"[{self.node_id}] {success_msg}")
            log_system_message(success_msg)
            return True
            
        except Exception as e:
            error_msg = f"Error sending email: {str(e)}"
            print(f"[{self.node_id}] {error_msg}")
            log_error(error_msg)
            return False
    
    def _create_message(self, to, subject, body):
        """Create a base64url encoded email message with proper formatting"""
        import base64
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        message = MIMEMultipart()
        message['to'] = to
        message['subject'] = subject
        
        # Format the body to preserve line breaks
        formatted_body = body
        
        # Add text body with proper content type to preserve formatting
        msg = MIMEText(formatted_body, 'plain', 'utf-8')
        message.attach(msg)
        
        # Encode the message
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        return raw_message

    def _send_email_after_confirmation(self):
        """Send the email after user confirmation"""
        recipient = self.email_context['collected_info'].get('recipient', '')
        subject = self.email_context['collected_info'].get('subject', '')
        body = self.email_context['collected_info'].get('body', '')
        
        # Validate that we have the minimum required information
        if not recipient or not body:
            print(f"[{self.node_id}] Response: Cannot send email - missing recipient or body content.")
            self.email_context['active'] = False
            return
            
        # If subject is empty, create a default one
        if not subject:
            # Use the first line of the body or a generic subject
            first_line = body.split('\n')[0]
            if len(first_line) > 5 and len(first_line) < 80:
                subject = first_line
            else:
                subject = "Message from " + self.node_id
        
        # If recipient is a name without email, try to resolve it
        if '@' not in recipient:
            # Check if it's a node name
            if recipient.lower() in ['ceo', 'marketing', 'engineering', 'design']:
                recipient = f"{recipient.lower()}@example.com"
            else:
                # Try to make a reasonable guess
                recipient = f"{recipient.replace(' ', '').lower()}@example.com"
        
        # Send the email
        success = self.send_email(recipient, subject, body)
        
        if success:
            print(f"[{self.node_id}] Response: Email sent successfully to {recipient}!")
        else:
            print(f"[{self.node_id}] Response: There was an error sending your email. Please try again later.")
            
        # Reset email context
        self.email_context['active'] = False

    def _detect_send_email_intent(self, message):
        """Detect if the message is requesting to send an email"""
        # Skip this detection if we're already in email composition mode
        if hasattr(self, 'email_context') and self.email_context.get('active'):
            return {"is_send_email": False}
            
        prompt = f"""
        Analyze this message and determine if it's requesting to send an email:
        "{message}"
        
        A message is considered an email sending request if:
        1. It contains phrases like "send email", "write email", "send mail", "compose email", "draft email", etc.
        2. There's a clear intention to create and send an email to someone

        Return JSON with:
        - is_send_email: boolean (true if the message is about sending an email)
        - recipient: string (email address or name of recipient if specified, empty string if not)
        - subject: string (email subject line if specified, empty string if not)
        - body: string (email content if specified, empty string if not)
        - missing_info: array of strings (what information is missing: "recipient", "subject", "body")

        Notes:
        - If the message contains phrases like "subject:" or "title:" followed by text, extract that as the subject
        - If the message has text after keywords like "body:", "content:", or "message:", extract that as the body
        - If it says "the subject is" or "subject is" followed by text, extract that as the subject
        - If it says "the body is" or "message is" followed by text, extract that as the body
        - If no explicit markers are present but there's a clear distinction between subject and body, make your best guess
        - Look for paragraph breaks or sentence structure to identify where subject ends and body begins
        - For recipient, extract just the name or email (don't include words like "to" or "for")
        - If the message itself appears to be the content of the email, set body to the entire message excluding obvious command parts
        """
        
        try:
            response = self.client.chat.completions.create(
                model="gpt-4.1",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            
            # Determine what information is missing
            missing = []
            if not result.get('recipient'):
                missing.append('recipient')
            if not result.get('subject'):
                missing.append('subject')
            if not result.get('body'):
                missing.append('body')
                
            result['missing_info'] = missing
            
            return result
        except Exception as e:
            print(f"[{self.node_id}] Error detecting send email intent: {str(e)}")
            return {"is_send_email": False, "recipient": "", "subject": "", "body": "", "missing_info": []}

    def _start_email_composition(self, initial_message, missing_info, email_data):
        """Start the email composition flow by asking for missing information"""
        # Initialize email context
        self.email_context = {
            'active': True,
            'initial_message': initial_message,
            'missing_info': missing_info.copy(),
            'collected_info': {
                'recipient': email_data.get('recipient', ''),
                'subject': email_data.get('subject', ''),
                'body': email_data.get('body', '')
            },
            'state': 'collecting_info'  # States: collecting_info, confirming, sending
        }
        
        # Ask for the first missing piece of information
        self._ask_for_next_email_info()
        
    def _ask_for_next_email_info(self):
        """Ask user for the next piece of missing email information"""
        if not self.email_context['missing_info']:
            # We have all the information, proceed to confirmation
            self._show_email_preview()
            return
        
        next_info = self.email_context['missing_info'][0]
        
        # Questions for each type of missing information
        questions = {
            'recipient': "To whom would you like to send this email? (Please provide an email address or name)",
            'subject': "What should be the subject of your email?",
            'body': "Please write the content of your email. You can include multiple paragraphs."
        }
        
        # Special case for subject when both subject and body are missing
        if next_info == 'subject' and 'body' in self.email_context['missing_info']:
            questions['subject'] = "What should be the subject and body of your email? You can provide both by saying something like 'The subject is X, body is Y'."
        
        response = questions.get(next_info, f"Please provide the {next_info} for the email")
        print(f"[{self.node_id}] Response: {response}")
        
    def _continue_email_composition(self, message, sender_id):
        """Process user's response to our question about email details"""
        if not self.email_context.get('active', False):
            return
            
        if not self.email_context.get('missing_info'):
            # We should be in confirmation state
            current_state = self.email_context.get('state', '')
            
            if current_state == 'confirming':
                # Process confirmation response
                if self._is_confirmation_positive(message):
                    # User confirmed, send the email
                    self._send_email_after_confirmation()
                else:
                    # User declined or response unclear
                    print(f"[{self.node_id}] Response: Email sending cancelled. You can start over or modify your request.")
                    self.email_context['active'] = False
            return
            
        current_state = self.email_context.get('state', '')
        
        if current_state == 'collecting_info':
            current_info = self.email_context['missing_info'].pop(0)
            
            # Special handling for different types of information
            if current_info == 'subject':
                # Check if the message includes both subject and body
                if 'body:' in message.lower() or 'message:' in message.lower() or 'content:' in message.lower():
                    # This response might contain both subject and body
                    # Let's parse it properly
                    parsed = self._parse_subject_and_body(message)
                    
                    # Store the parsed subject
                    self.email_context['collected_info']['subject'] = parsed.get('subject', message)
                    
                    # If body was also included, store it and remove from missing info
                    if parsed.get('body') and 'body' in self.email_context['missing_info']:
                        self.email_context['collected_info']['body'] = parsed.get('body')
                        self.email_context['missing_info'].remove('body')
                else:
                    # Just a simple subject
                    self.email_context['collected_info']['subject'] = message
            
            elif current_info == 'body':
                # Preserve formatting in the body
                self.email_context['collected_info']['body'] = message
            
            else:
                # Default handling for other fields
                self.email_context['collected_info'][current_info] = message
            
            if self.email_context['missing_info']:
                # Still need more information
                self._ask_for_next_email_info()
            else:
                # We have all the information, show preview
                self._show_email_preview()
                
        elif current_state == 'confirming':
            # Process confirmation response
            if self._is_confirmation_positive(message):
                # User confirmed, send the email
                self._send_email_after_confirmation()
            else:
                # User declined or response unclear
                print(f"[{self.node_id}] Response: Email sending cancelled. You can start over or modify your request.")
                self.email_context['active'] = False
                
    def _parse_subject_and_body(self, message):
        """Parse a message that might contain both subject and body"""
        # Check for common patterns first
        subject_body_pattern = re.search(r"(?i)the\s+subject\s+is\s+[\"']?(.*?)[\"']?,?\s+(?:the\s+)?body(?:\s+message)?\s+is\s+[\"']?(.*?)[\"']?$", message)
        if subject_body_pattern:
            subject = subject_body_pattern.group(1).strip()
            body = subject_body_pattern.group(2).strip()
            return {'subject': subject, 'body': body}
            
        # Also check for subject: and body: pattern
        if re.search(r"(?i)subject:", message) and re.search(r"(?i)body:", message):
            parts = re.split(r"(?i)body:", message, 1)
            subject_part = parts[0]
            body_part = parts[1].strip()
            
            # Extract subject after "subject:"
            subject_match = re.search(r"(?i)subject:(.*?)(?:$|,|\n)", subject_part)
            if subject_match:
                subject = subject_match.group(1).strip()
                return {'subject': subject, 'body': body_part}
        
        # Use AI for more complex parsing if simple patterns don't match
        prompt = f"""
        Parse this message to extract the email subject and body:
        "{message}"
        
        Look for patterns like:
        - "Subject:" or "The subject is" followed by text (for subject)
        - "Body:" or "Message:" or "Content:" followed by text (for body)
        - Clear paragraph breaks or keywords indicating separate sections
        
        Return a JSON object with:
        - subject: the extracted subject line
        - body: the extracted email body
        
        If either cannot be clearly identified, return an empty string for that field.
        """
        
        try:
            response = self.client.chat.completions.create(
                model="gpt-4.1",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.1  # Lower temperature for more consistent parsing
            )
            
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f"[{self.node_id}] Error parsing subject and body: {str(e)}")
            # If parsing fails, return the full message as subject
            return {'subject': message, 'body': ''}

    def _is_confirmation_positive(self, message):
        """Check if the user's response is a confirmation to send the email"""
        positive_indicators = [
            'yes', 'yeah', 'yep', 'yup', 'sure', 'ok', 'okay', 'fine', 
            'send', 'send it', 'send email', 'send the email',
            'confirm', 'confirmed', 'confirmation', 'approve', 'approved',
            'go ahead', 'proceed', 'do it', 'looks good'
        ]
        negative_indicators = [
            'no', 'nope', 'don\'t', 'do not', 'cancel', 'stop', 'abort',
            'don\'t send', 'do not send', 'wait', 'hold on', 'nevermind'
        ]
        
        message_lower = message.lower().strip()
        
        # Check for explicit negative response first
        if any(indicator in message_lower for indicator in negative_indicators):
            return False
            
        # Then check for positive response
        return any(indicator in message_lower for indicator in positive_indicators)
                
    def _show_email_preview(self):
        """Show a preview of the email and ask for confirmation"""
        self.email_context['state'] = 'confirming'
        
        recipient = self.email_context['collected_info'].get('recipient', '')
        subject = self.email_context['collected_info'].get('subject', '')
        body = self.email_context['collected_info'].get('body', '')
        
        # Format the preview nicely
        preview = (
            f"📧 Email Preview 📧\n\n"
            f"To: {recipient}\n"
            f"Subject: {subject or '(No subject)'}\n"
            f"---\n"
            f"{body}\n"
            f"---\n\n"
            f"Would you like me to send this email? (Yes/No)"
        )
        
        print(f"[{self.node_id}] Response: {preview}")


def run_cli(network):
    print("Commands:\n"
          "  node_id: message => send 'message' to 'node_id' from CLI\n"
          "  node_id: plan project_name = objective => create a new project plan\n"
          "  node_id: tasks => list tasks for a node\n"
          "  quit => exit\n")

    while True:
        user_input = input("> ")
        if user_input.lower().strip() == "quit":
            print("Exiting chat...\n")
            print("\n===== Final State of Each Node =====")
            for node_id, node in network.nodes.items():
                print(f"\n--- Node: {node_id} ---")
                print("Calendar:", node.calendar)
                print("Projects:", node.projects)
                print("Tasks:", network.get_tasks_for_node(node_id))
                print("Conversation History:", node.conversation_history)
            break

        # Plan project command
        if "plan" in user_input and "=" in user_input:
            try:
                # e.g. "ceo: plan p123 = Build AI feature"
                parts = user_input.split(":", 1)
                if len(parts) != 2:
                    print("Invalid format. Use: node_id: plan project_name = objective")
                    continue
                    
                node_id = parts[0].strip()
                command_part = parts[1].strip()
                
                # Extract everything after "plan" keyword
                if "plan" not in command_part:
                    print("Command must include the word 'plan'")
                    continue
                    
                plan_part = command_part.split("plan", 1)[1].strip()
                
                if "=" not in plan_part:
                    print("Invalid format. Missing '=' between project name and objective")
                    continue
                    
                project_id_part, objective_part = plan_part.split("=", 1)
                project_id = project_id_part.strip()
                objective = objective_part.strip()

                if node_id in network.nodes:
                    network.nodes[node_id].plan_project(project_id, objective)
                else:
                    print(f"No node found: {node_id}")
            except Exception as e:
                print(f"Error parsing plan command: {str(e)}")
        # List tasks command
        elif "tasks" in user_input:
            try:
                node_id = user_input.split(":", 1)[0].strip()
                if node_id in network.nodes:
                    tasks_list = network.nodes[node_id].list_tasks()
                    print(tasks_list)
                else:
                    print(f"No node found: {node_id}")
            except Exception as e:
                print(f"Error listing tasks: {e}")
        else:
            # normal message command: "node_id: some message"
            if ":" not in user_input:
                print("Invalid format. Use:\n  node_id: message\nOR\n  node_id: plan project_name = objective\nOR\n  node_id: tasks\n")
                continue
            node_id, message = user_input.split(":", 1)
            node_id = node_id.strip()
            message = message.strip()

            if node_id in network.nodes:
                # The CLI user sends a message to the node
                network.nodes[node_id].receive_message(message, "cli_user")
            else:
                print(f"No node with ID '{node_id}' found.")


# Modify the Flask app initialization
app = Flask(__name__, template_folder='UI')
CORS(app)  # Enable CORS for all routes

# Initialize SocketIO, allowing connections from any origin for development
socketio = SocketIO(app, cors_allowed_origins="*")

network = None  # Will be set by the main function

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/tasks')
def show_tasks():
    global network
    if not network:
        return jsonify({"error": "Network not initialized"}), 500
    
    all_tasks = []
    for node_id, node in network.nodes.items():
        tasks = network.get_tasks_for_node(node_id)
        for task in tasks:
            all_tasks.append(task.to_dict())
    
    return jsonify(all_tasks)

@app.route('/nodes')
def show_nodes():
    global network
    if not network:
        return jsonify({"error": "Network not initialized"}), 500
    
    nodes = list(network.nodes.keys())
    return jsonify(nodes)

@app.route('/projects')
def show_projects():
    global network
    if not network:
        return jsonify({"error": "Network not initialized"}), 500
    
    all_projects = {}
    for node_id, node in network.nodes.items():
        for project_id, project in node.projects.items():
            if project_id not in all_projects:
                all_projects[project_id] = {
                    "name": project.get("name", ""),
                    "participants": list(project.get("participants", set())),
                    "owner": node_id
                }
    
    return jsonify(all_projects)

@app.route('/transcribe_audio', methods=['POST'])
def transcribe_audio():
    global network
    if not network:
        return jsonify({"error": "Network not initialized"}), 500
    
    data = request.json
    node_id = data.get('node_id')
    audio_data = data.get('audio_data')
    
    if not node_id or not audio_data:
        return jsonify({"error": "Missing node_id or audio_data"}), 400
    
    if node_id not in network.nodes:
        return jsonify({"error": f"Node {node_id} not found"}), 404
    
    # Decode the base64 audio data
    try:
        # Remove the data URL prefix if present
        if 'base64,' in audio_data:
            audio_data = audio_data.split('base64,')[1]
        
        audio_bytes = base64.b64decode(audio_data)
        
        # Save to a temporary file with mp3 extension
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as temp_file:
            temp_file_path = temp_file.name
            temp_file.write(audio_bytes)
        
        print(f"[DEBUG] Audio file saved to {temp_file_path} with size {len(audio_bytes)} bytes")
    
        
        # Use Whisper API for transcription
        with open(temp_file_path, 'rb') as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="en",
                response_format="text"
            )
        
        # Clean up the temporary file
        os.unlink(temp_file_path)
        
        # Log the transcript for debugging
        print(f"[DEBUG] Whisper transcription: {transcript}")
        
        command_text = transcript
    
        # Use the same process as sending a text message
        response_collector = {"response": None, "terminal_output": []}
        
        # Override the print function temporarily to capture all output
        original_print = print
        
        def custom_print(text):
            if isinstance(text, str):
                # Capture all terminal output
                response_collector["terminal_output"].append(text)
                
                # Also capture the direct response
                if text.startswith(f"[{node_id}] Response: "):
                    response_collector["response"] = text.replace(f"[{node_id}] Response: ", "")
            original_print(text)
        
        # Replace print function
        import builtins
        builtins.print = custom_print
        
        try:
            # Send the message to the node
            network.nodes[node_id].receive_message(command_text, "cli_user")
            
            # Restore original print function
            builtins.print = original_print
            
            # Format terminal output for display
            terminal_text = "\n".join(response_collector["terminal_output"])
            
            # Generate speech from the response
            audio_response = None
            if response_collector["response"]:
                try:
                    speech_response = client.audio.speech.create(
                        model="tts-1",
                        voice="alloy",
                        input=response_collector["response"]
                    )
                    
                    # Convert to base64 for sending to the client
                    speech_response.write_to_file("temp_speech.mp3") 
                    
                    with open("temp_speech.mp3", "rb") as audio_file:
                        audio_response = base64.b64encode(audio_file.read()).decode('utf-8')
                    os.unlink("temp_speech.mp3")
                except Exception as e:
                    print(f"Error generating speech: {str(e)}")
            
            return jsonify({
                "response": response_collector["response"],
                "terminal_output": terminal_text,
                "transcription": command_text,
                "audio_response": audio_response
            })
            
        except Exception as e:
            # Restore original print function
            builtins.print = original_print
            return jsonify({"error": str(e)}), 500
            
    except Exception as e:
        print(f"[DEBUG] Error in audio processing: {str(e)}")
        return jsonify({"error": f"Error processing audio: {str(e)}"}), 500

# Update the existing send_message route to use the common function
@app.route('/send_message', methods=['POST'])
def send_message():
    global network
    if not network:
        return jsonify({"error": "Network not initialized"}), 500
    
    data = request.json
    node_id = data.get('node_id')
    message = data.get('message')
    
    if not node_id or not message:
        return jsonify({"error": "Missing node_id or message"}), 400
    
    if node_id not in network.nodes:
        return jsonify({"error": f"Node {node_id} not found"}), 404
    
    return send_message_internal(node_id, message)

def send_message_internal(node_id, message):
    """Process a message sent to a node and return captured response"""
    # Collector for response and terminal output
    response_collector = {"response": None, "terminal_output": []}
    
    # Override the print function temporarily to capture output
    original_print = print
    
    def custom_print(text):
        if isinstance(text, str):
            # Capture all terminal output
            response_collector["terminal_output"].append(text)
            
            # Also capture the direct response
            if text.startswith(f"[{node_id}] Response: "):
                response_collector["response"] = text.replace(f"[{node_id}] Response: ", "")
        original_print(text)
    
    # Replace print function
    import builtins
    builtins.print = custom_print
    
    try:
        # Send the message to the node
        network.nodes[node_id].receive_message(message, "cli_user")
        
        # Restore original print function
        builtins.print = original_print
        
        # Format terminal output for display
        terminal_text = "\n".join(response_collector["terminal_output"])
        
        return jsonify({
            "response": response_collector["response"],
            "terminal_output": terminal_text
        })
        
    except Exception as e:
        # Restore original print function
        builtins.print = original_print
        return jsonify({"error": str(e)}), 500

def start_flask():
    # Try different ports if 5000 is in use
    for port in range(5001, 5010):
        try:
            # Use socketio.run instead of app.run
            print(f"Attempting to start SocketIO server on port {port}")
            # Add allow_unsafe_werkzeug=True if needed for development auto-reloader with SocketIO
            socketio.run(app, debug=False, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True) 
            print(f"SocketIO server started successfully on port {port}")
            break # Exit loop if successful
        except OSError as e:
            if 'Address already in use' in str(e):
                print(f"Port {port} is in use, trying next port...")
            else:
                print(f"An unexpected OS error occurred: {e}")
                break # Stop trying if it's not an address-in-use error
        except Exception as e:
            print(f"An unexpected error occurred trying to start the server: {e}")
            break # Stop trying on other errors

def open_browser():
    # Wait a bit for Flask to start
    import time
    time.sleep(1.5)
    # Try different ports
    for port in range(5001, 5010):
        try:
            # Try to connect to check if this is the port being used
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('127.0.0.1', port))
            sock.close()
            if result == 0:  # Port is open, server is running here
                webbrowser.open(f'http://localhost:{port}')
                break
        except:
            continue

def demo_run():
    global network
    network = Network(log_file="communication_log.txt")

    # Create nodes
    ceo = LLMNode("ceo", knowledge="Knows entire org structure.")
    marketing = LLMNode("marketing", knowledge="Knows about markets.")
    engineering = LLMNode("engineering", knowledge="Knows codebase.")
    design = LLMNode("design", knowledge="Knows UI/UX best practices.")

    # Register them
    network.register_node(ceo)
    network.register_node(marketing)
    network.register_node(engineering)
    network.register_node(design)

    # Start Flask (which now uses SocketIO) in a separate thread
    flask_thread = threading.Thread(target=start_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Open browser automatically
    browser_thread = threading.Thread(target=open_browser)
    browser_thread.daemon = True
    browser_thread.start()

    # Start the CLI
    run_cli(network)


if __name__ == "__main__":
    # Make sure network is initialized before flask starts using it
    network = Network(log_file="communication_log.txt")

    # Create nodes
    ceo = LLMNode("ceo", knowledge="Knows entire org structure.")
    marketing = LLMNode("marketing", knowledge="Knows about markets.")
    engineering = LLMNode("engineering", knowledge="Knows codebase.")
    design = LLMNode("design", knowledge="Knows UI/UX best practices.")

    # Register them
    network.register_node(ceo)
    network.register_node(marketing)
    network.register_node(engineering)
    network.register_node(design)

    # Start Flask (which now uses SocketIO) in a separate thread
    flask_thread = threading.Thread(target=start_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # Open browser automatically
    browser_thread = threading.Thread(target=open_browser)
    browser_thread.daemon = True
    browser_thread.start()

    # Start the CLI
    run_cli(network)

# --- Add CV Upload Route ---
@app.route('/upload_cv', methods=['POST'])
def upload_cv_route():
    if 'cv_file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    file = request.files['cv_file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    
    # Ensure the filename ends with .pdf (case-insensitive)
    if file and '.' in file.filename and file.filename.rsplit('.', 1)[1].lower() == 'pdf':
        temp_file_path = None # Initialize path variable
        try:
            # Create a temporary file to store the PDF
            with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as temp_pdf:
                file.save(temp_pdf.name)
                temp_file_path = temp_pdf.name
            
            print(f"[CV Parser] Temporary file saved at: {temp_file_path}")

            #Instantiate the parser and parse the CV
            # Ensure OPENAI_API_KEY is available in the environment where CVParser runs
            #parser = CVParser() 
            #cv_data = parser.parse_cv(temp_file_path)
        
            # Check if parsing was successful
            #if cv_data is None:
            #    print("[CV Parser] Parsing failed, cv_data is None.")
            #    return jsonify({"error": "Could not parse CV file. Check server logs for details."}), 500
            
            # Return the extracted data successfully
            print("[CV Parser] Parsing successful.")
            return jsonify({
                'success': True, 
            #    'summary': cv_data # Contains name, email, phone, education, work_experience, skills
            }), 200

        except Exception as e:
            # Log the error for debugging
            print(f"[CV Parser] Error processing CV: {str(e)}")
            # Return a generic error message to the client
            return jsonify({'error': f"An unexpected error occurred while processing the CV."}), 500
        finally:
            # --- Ensure temporary file cleanup --- 
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                    print(f"[CV Parser] Temporary file deleted: {temp_file_path}")
                except Exception as cleanup_e:
                    # Log cleanup error but don't necessarily fail the request
                    print(f"[CV Parser] Error deleting temp file during cleanup: {cleanup_e}")
            # --- End cleanup --- 
        
    else:
        # File is not a PDF or has no extension
        return jsonify({"error": "Invalid file type. Please upload a PDF file."}), 400
# --- End CV Upload Route ---
