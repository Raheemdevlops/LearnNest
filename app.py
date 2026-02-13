import os
import sqlite3
import logging
from datetime import datetime, timedelta
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, send_from_directory, send_file
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_socketio import SocketIO, emit, join_room, leave_room, rooms
from flask_caching import Cache
from flask_mail import Mail, Message
from threading import Lock
import json
import uuid
from dotenv import load_dotenv
import gemini_ai
from pytubefix import YouTube
import shutil

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SESSION_SECRET') or 'dev-secret-key-change-in-production'
# Use paths relative to the app file location
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'sir_rafique', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = None  # No file size limit for videos

# Database configuration
DATABASE = os.path.join(BASE_DIR, 'sir_rafique', 'learnnest.db')

# Initialize extensions
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'  # type: ignore
login_manager.login_message = 'Please log in to access this page.'

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Initialize caching
cache = Cache(app, config={'CACHE_TYPE': 'simple'})

# Initialize mail
mail = Mail(app)

# Register custom Jinja filters and globals
@app.template_filter('nl2br')
def nl2br_filter(s):
    """Convert newlines to HTML line breaks"""
    if s is None:
        return ''
    return str(s).replace('\n', '<br>\n')

# Add custom Jinja2 global functions
@app.template_global()
def max_func(*args):
    """Max function for Jinja2 templates"""
    return max(args)

@app.template_global()
def min_func(*args):
    """Min function for Jinja2 templates"""
    return min(args)

# CSRF protection helpers
import secrets
import hashlib

def generate_csrf_token():
    """Generate a CSRF token for forms"""
    token = secrets.token_urlsafe(32)
    session['csrf_token'] = token
    return token

def validate_csrf_token(token):
    """Validate CSRF token"""
    if not token or token != session.get('csrf_token'):
        return False
    return True

@app.context_processor
def inject_csrf_token():
    """Make CSRF token available in all templates"""
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)
    return dict(csrf_token=session['csrf_token'])

# Thread lock for database operations
db_lock = Lock()

# Create uploads directory
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'assignments'), exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'resources'), exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'payments'), exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'instructor_screenshots'), exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'transcripts'), exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'chat_files'), exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'chat_images'), exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'direct_messages'), exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'forum_media'), exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'profile_pictures'), exist_ok=True)

# File size limits (in bytes)
MAX_CHAT_FILE_SIZE = 100 * 1024 * 1024  # 100 MB per file
MAX_TOTAL_STORAGE_PER_USER = 5 * 1024 * 1024 * 1024  # 5 GB per user
ALLOWED_FILE_TYPES = {
    'pdf', 'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'txt', 
    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg',
    'zip', 'rar', '7z', 'tar', 'gz',
    'mp4', 'mov', 'avi', 'mkv', 'webm', 'flv',
    'mp3', 'wav', 'ogg', 'm4a',
    'csv', 'json', 'xml', 'html', 'css', 'js', 'py', 'java', 'cpp', 'c', 'h'
}

# Database connection helper
def get_db_connection():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA synchronous=NORMAL;')
    conn.execute('PRAGMA cache_size=10000;')
    conn.execute('PRAGMA temp_store=MEMORY;')
    conn.row_factory = sqlite3.Row
    return conn

def send_notification(user_id, title, message, notification_type='info', related_id=None):
    """Helper function to send notifications to students"""
    try:
        conn = get_db_connection()
        conn.execute('''
            INSERT INTO notifications (user_id, title, message, type, related_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, title, message, notification_type, related_id, datetime.now()))
        conn.commit()
        conn.close()
        
        # Emit real-time notification via SocketIO
        socketio.emit('notification', {
            'title': title,
            'message': message,
            'type': notification_type
        }, to=f'user_{user_id}')
        
        return True
    except Exception as e:
        logging.error(f"Error sending notification: {e}")
        return False

def update_student_progress(conn, student_id, course_id):
    """
    Calculate and update student progress for a course based on quiz completions
    Progress = (Number of Submitted Quizzes / Total Quizzes) * 100
    Note: If manual_progress_override is set by instructor, automatic progress is still calculated and stored
          but the manual override value takes precedence for display
    """
    try:
        # Check if instructor has set manual progress override
        enrollment = conn.execute('''
            SELECT manual_progress_override
            FROM enrollments
            WHERE student_id = ? AND course_id = ?
        ''', (student_id, course_id)).fetchone()
        
        has_manual_override = enrollment and enrollment['manual_progress_override'] is not None
        
        # Get total number of quizzes for this course
        total_quizzes = conn.execute('''
            SELECT COUNT(*) as count
            FROM assignments
            WHERE course_id = ? AND assignment_type = 'quiz' AND is_active = 1
        ''', (course_id,)).fetchone()['count']
        
        if total_quizzes == 0:
            # No quizzes, set progress to 0
            auto_progress = 0
        else:
            # Get number of submitted quizzes by student
            submitted_quizzes = conn.execute('''
                SELECT COUNT(DISTINCT a.id) as count
                FROM assignments a
                INNER JOIN assignment_submissions sub ON a.id = sub.assignment_id
                WHERE a.course_id = ? 
                AND a.assignment_type = 'quiz'
                AND a.is_active = 1
                AND sub.student_id = ?
            ''', (course_id, student_id)).fetchone()['count']
            
            # Calculate progress percentage
            auto_progress = (submitted_quizzes / total_quizzes) * 100
        
        # Always update progress_percentage with automatic calculation
        # This keeps it in sync even when manual override is active
        conn.execute('''
            UPDATE enrollments
            SET progress_percentage = ?
            WHERE student_id = ? AND course_id = ?
        ''', (auto_progress, student_id, course_id))
        
        conn.commit()
        
        # Return manual override if set, otherwise return automatic progress
        if has_manual_override:
            return enrollment['manual_progress_override']
        else:
            return auto_progress
        
    except Exception as e:
        print(f"Error updating student progress: {e}")
        return 0

# User class for Flask-Login
class User(UserMixin):
    def __init__(self, id, username, email, role, full_name, created_at, active_status=True, 
                 instructor_approval_status='approved', approved_by=None, approved_at=None, profile_picture=None):
        self.id = id
        self.username = username
        self.email = email
        self.role = role
        self.full_name = full_name
        self.created_at = created_at
        self._is_active = active_status
        self.instructor_approval_status = instructor_approval_status
        self.approved_by = approved_by
        self.approved_at = approved_at
        self.profile_picture = profile_picture

    def get_id(self):
        return str(self.id)

    def is_admin(self):
        return self.role == 'admin'
    
    def is_instructor(self):
        return self.role == 'instructor'
    
    def is_student(self):
        return self.role == 'student'
    
    def is_instructor_approved(self):
        """Check if instructor is approved to access instructor features"""
        if not self.is_instructor():
            return True  # Non-instructors don't need approval
        return self.instructor_approval_status == 'approved'
    
    def is_instructor_pending(self):
        """Check if instructor is pending approval"""
        return self.is_instructor() and self.instructor_approval_status == 'pending'
    
    def is_instructor_rejected(self):
        """Check if instructor was rejected"""
        return self.is_instructor() and self.instructor_approval_status == 'rejected'

@login_manager.user_loader
def load_user(user_id):
    try:
        with db_lock:
            conn = get_db_connection()
            user = conn.execute(
                'SELECT * FROM users WHERE id = ? AND is_active = 1', (user_id,)
            ).fetchone()
            conn.close()
        
        if user:
            profile_pic = user['profile_picture'] if 'profile_picture' in user.keys() else None
            return User(user['id'], user['username'], user['email'], user['role'], 
                       user['full_name'], user['created_at'], user['is_active'],
                       user['instructor_approval_status'] if user['instructor_approval_status'] else 'approved',
                       user['approved_by'],
                       user['approved_at'],
                       profile_pic)
    except sqlite3.OperationalError:
        # Database not yet initialized
        pass
    return None

# Role-based access control decorators
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin():
            flash('Access denied. Admin privileges required.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

def instructor_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Access denied. Please log in.', 'error')
            return redirect(url_for('login'))
        
        # Admins always have access to instructor features
        if current_user.is_admin():
            return f(*args, **kwargs)
        
        # Check if user is instructor
        if not current_user.is_instructor():
            flash('Access denied. Instructor privileges required.', 'error')
            return redirect(url_for('dashboard'))
        
        # Check if instructor is approved
        if not current_user.is_instructor_approved():
            flash('Your instructor account is pending approval. Please wait for admin approval.', 'warning')
            return redirect(url_for('dashboard'))
        
        return f(*args, **kwargs)
    return decorated_function

# Database initialization
def init_db():
    print("🔄 Initializing database...")
    with db_lock:
        try:
            conn = get_db_connection()
            
            # Users table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'student',
                    full_name TEXT NOT NULL,
                    bio TEXT,
                    profile_image TEXT,
                    instructor_approval_status TEXT DEFAULT 'approved',
                    approved_by INTEGER,
                    approved_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY (approved_by) REFERENCES users (id)
                )
            ''')
            
            # Add columns if they don't exist
            try:
                conn.execute('ALTER TABLE users ADD COLUMN instructor_approval_status TEXT DEFAULT "approved"')
            except sqlite3.OperationalError:
                pass
            
            try:
                conn.execute('ALTER TABLE users ADD COLUMN approved_by INTEGER')
            except sqlite3.OperationalError:
                pass
                
            try:
                conn.execute('ALTER TABLE users ADD COLUMN approved_at TIMESTAMP')
            except sqlite3.OperationalError:
                pass
                
            try:
                conn.execute('ALTER TABLE courses ADD COLUMN enrollment_key_hash TEXT')
            except sqlite3.OperationalError:
                pass
            
            try:
                conn.execute('ALTER TABLE users ADD COLUMN instructor_screenshot TEXT')
            except sqlite3.OperationalError:
                pass
            
            # Courses table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS courses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_code TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    syllabus TEXT,
                    instructor_id INTEGER NOT NULL,
                    category TEXT,
                    max_students INTEGER DEFAULT 50,
                    start_date DATE,
                    end_date DATE,
                    enrollment_key_hash TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY (instructor_id) REFERENCES users (id)
                )
            ''')
            
            # Enrollments table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS enrollments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL,
                    course_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    payment_screenshot TEXT,
                    enrolled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    approved_at TIMESTAMP,
                    progress_percentage REAL DEFAULT 0,
                    manual_progress_override REAL DEFAULT NULL,
                    FOREIGN KEY (student_id) REFERENCES users (id),
                    FOREIGN KEY (course_id) REFERENCES courses (id),
                    UNIQUE(student_id, course_id)
                )
            ''')
            
            # Add manual_progress_override column if it doesn't exist
            try:
                conn.execute('ALTER TABLE enrollments ADD COLUMN manual_progress_override REAL DEFAULT NULL')
            except sqlite3.OperationalError:
                pass
            
            # Assignments table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    instructions TEXT,
                    due_date DATETIME,
                    max_points INTEGER DEFAULT 100,
                    allow_late_submission BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (course_id) REFERENCES courses (id)
                )
            ''')
            
            # Add new columns to assignments table for advanced features
            try:
                conn.execute('ALTER TABLE assignments ADD COLUMN assignment_type TEXT DEFAULT "quiz"')
            except sqlite3.OperationalError:
                pass
            
            try:
                conn.execute('ALTER TABLE assignments ADD COLUMN status TEXT DEFAULT "draft"')
            except sqlite3.OperationalError:
                pass
            
            try:
                conn.execute('ALTER TABLE assignments ADD COLUMN published_at TIMESTAMP')
            except sqlite3.OperationalError:
                pass
            
            try:
                conn.execute('ALTER TABLE assignments ADD COLUMN ai_context TEXT')
            except sqlite3.OperationalError:
                pass
            
            # Assignment assets table for file uploads
            conn.execute('''
                CREATE TABLE IF NOT EXISTS assignment_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    assignment_id INTEGER NOT NULL,
                    file_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_type TEXT,
                    file_size INTEGER,
                    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (assignment_id) REFERENCES assignments (id)
                )
            ''')
            
            # Assignment submissions table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS assignment_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    assignment_id INTEGER NOT NULL,
                    student_id INTEGER NOT NULL,
                    submission_text TEXT,
                    file_path TEXT,
                    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    grade REAL,
                    ai_feedback TEXT,
                    instructor_feedback TEXT,
                    graded_at TIMESTAMP,
                    FOREIGN KEY (assignment_id) REFERENCES assignments (id),
                    FOREIGN KEY (student_id) REFERENCES users (id),
                    UNIQUE(assignment_id, student_id)
                )
            ''')
            
            # Quiz questions table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS quiz_questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    assignment_id INTEGER NOT NULL,
                    question_text TEXT NOT NULL,
                    question_type TEXT NOT NULL DEFAULT 'mcq',
                    points INTEGER DEFAULT 1,
                    correct_answer TEXT,
                    explanation TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (assignment_id) REFERENCES assignments (id)
                )
            ''')
            
            # Question options table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS question_options (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_id INTEGER NOT NULL,
                    option_letter TEXT NOT NULL,
                    option_text TEXT NOT NULL,
                    is_correct BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (question_id) REFERENCES quiz_questions (id)
                )
            ''')
            
            # Student MCQ answers table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS student_mcq_answers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submission_id INTEGER NOT NULL,
                    question_id INTEGER NOT NULL,
                    selected_option TEXT,
                    is_correct BOOLEAN,
                    points_earned REAL DEFAULT 0,
                    answered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (submission_id) REFERENCES assignment_submissions (id),
                    FOREIGN KEY (question_id) REFERENCES quiz_questions (id),
                    UNIQUE(submission_id, question_id)
                )
            ''')
            
            # Forums table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS forums (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY (course_id) REFERENCES courses (id)
                )
            ''')
            
            # Forum topics table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS forum_topics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    forum_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_pinned BOOLEAN DEFAULT 0,
                    view_count INTEGER DEFAULT 0,
                    FOREIGN KEY (forum_id) REFERENCES forums (id),
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            ''')
            
            # Forum replies table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS forum_replies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_ai_generated BOOLEAN DEFAULT 0,
                    FOREIGN KEY (topic_id) REFERENCES forum_topics (id),
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            ''')
            
            # Chat messages table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id INTEGER,
                    sender_id INTEGER NOT NULL,
                    recipient_id INTEGER,
                    message TEXT NOT NULL,
                    message_type TEXT DEFAULT 'text',
                    file_path TEXT,
                    file_name TEXT,
                    file_size INTEGER DEFAULT 0,
                    is_image BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_read BOOLEAN DEFAULT 0,
                    FOREIGN KEY (course_id) REFERENCES courses (id),
                    FOREIGN KEY (sender_id) REFERENCES users (id),
                    FOREIGN KEY (recipient_id) REFERENCES users (id)
                )
            ''')
            
            # Direct messages table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS direct_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id INTEGER NOT NULL,
                    recipient_id INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    message_type TEXT DEFAULT 'text',
                    file_path TEXT,
                    file_name TEXT,
                    is_image BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_read BOOLEAN DEFAULT 0,
                    FOREIGN KEY (sender_id) REFERENCES users (id),
                    FOREIGN KEY (recipient_id) REFERENCES users (id)
                )
            ''')
            
            # File uploads table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS file_uploads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uploader_id INTEGER NOT NULL,
                    file_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_size INTEGER,
                    file_type TEXT,
                    message_id INTEGER,
                    direct_message_id INTEGER,
                    forum_reply_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (uploader_id) REFERENCES users (id),
                    FOREIGN KEY (message_id) REFERENCES chat_messages (id),
                    FOREIGN KEY (direct_message_id) REFERENCES direct_messages (id),
                    FOREIGN KEY (forum_reply_id) REFERENCES forum_replies (id)
                )
            ''')
            
            # Notifications table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    type TEXT DEFAULT 'info',
                    related_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_read BOOLEAN DEFAULT 0,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            ''')
            
            # Course resources table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS course_resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    file_path TEXT,
                    file_type TEXT,
                    upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    uploaded_by INTEGER NOT NULL,
                    FOREIGN KEY (course_id) REFERENCES courses (id),
                    FOREIGN KEY (uploaded_by) REFERENCES users (id)
                )
            ''')
            
            # Course meeting links table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS course_meeting_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    meeting_link TEXT NOT NULL,
                    description TEXT,
                    scheduled_time TIMESTAMP,
                    created_by INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY (course_id) REFERENCES courses (id),
                    FOREIGN KEY (created_by) REFERENCES users (id)
                )
            ''')
            
            # Course video playlists table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS course_video_playlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    video_url TEXT NOT NULL,
                    description TEXT,
                    thumbnail_url TEXT,
                    duration TEXT,
                    notes_file_path TEXT,
                    order_index INTEGER DEFAULT 0,
                    created_by INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY (course_id) REFERENCES courses (id),
                    FOREIGN KEY (created_by) REFERENCES users (id)
                )
            ''')
            
            # Add transcript column if needed
            try:
                conn.execute('ALTER TABLE course_video_playlists ADD COLUMN transcript_file_path TEXT')
            except sqlite3.OperationalError:
                pass
            
            # Student video playlists table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS student_video_playlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    video_url TEXT NOT NULL,
                    description TEXT,
                    thumbnail_url TEXT,
                    duration TEXT,
                    order_index INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY (student_id) REFERENCES users (id)
                )
            ''')
            
            # AI Notes table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS ai_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id INTEGER NOT NULL,
                    topic TEXT NOT NULL,
                    content TEXT NOT NULL,
                    pdf_path TEXT,
                    created_by INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_instructor_note BOOLEAN DEFAULT 0,
                    sent_to_students BOOLEAN DEFAULT 0,
                    FOREIGN KEY (course_id) REFERENCES courses (id),
                    FOREIGN KEY (created_by) REFERENCES users (id)
                )
            ''')
            
            # Add media columns to forum_topics if needed
            try:
                conn.execute('ALTER TABLE forum_topics ADD COLUMN media_type TEXT')
            except sqlite3.OperationalError:
                pass
            
            try:
                conn.execute('ALTER TABLE forum_topics ADD COLUMN media_path TEXT')
            except sqlite3.OperationalError:
                pass
            
            try:
                conn.execute('ALTER TABLE forum_topics ADD COLUMN media_filename TEXT')
            except sqlite3.OperationalError:
                pass
            
            # Add media columns to forum_replies if needed
            try:
                conn.execute('ALTER TABLE forum_replies ADD COLUMN media_type TEXT')
            except sqlite3.OperationalError:
                pass
            
            try:
                conn.execute('ALTER TABLE forum_replies ADD COLUMN media_path TEXT')
            except sqlite3.OperationalError:
                pass
            
            try:
                conn.execute('ALTER TABLE forum_replies ADD COLUMN media_filename TEXT')
            except sqlite3.OperationalError:
                pass
            
            # Create indexes
            conn.execute('CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_users_approval_status ON users(instructor_approval_status)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_enrollments_student ON enrollments(student_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_enrollments_course ON enrollments(course_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_assignments_course ON assignments(course_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_submissions_assignment ON assignment_submissions(assignment_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_quiz_questions_assignment ON quiz_questions(assignment_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_question_options_question ON question_options(question_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_student_answers_submission ON student_mcq_answers(submission_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_student_answers_question ON student_mcq_answers(question_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_course ON chat_messages(course_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_meeting_links_course ON course_meeting_links(course_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_video_playlists_course ON course_video_playlists(course_id)')
            
            # Create default admin user
            admin_exists = conn.execute('SELECT id FROM users WHERE role = "admin"').fetchone()
            if not admin_exists:
                admin_password = generate_password_hash('admin123')
                conn.execute('''
                    INSERT INTO users (username, email, password_hash, role, full_name, bio)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', ('admin', 'admin@learnnest.com', admin_password, 'admin', 
                     'System Administrator', 'Default system administrator account'))
            
            conn.commit()
            conn.close()
            print("✅ Database initialized successfully!")
            return True
        except Exception as e:
            print(f"❌ Database initialization error: {e}")
            import traceback
            traceback.print_exc()
            return False

# Routes
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        # Validate CSRF token
        csrf_token = request.form.get('csrf_token')
        if not validate_csrf_token(csrf_token):
            flash('Invalid security token. Please try again.', 'error')
            return render_template('auth/login.html')
            
        email = request.form['email'].lower().strip()
        password = request.form['password']
        remember = bool(request.form.get('remember'))
        
        with db_lock:
            conn = get_db_connection()
            user = conn.execute(
                'SELECT * FROM users WHERE email = ? AND is_active = 1', (email,)
            ).fetchone()
            conn.close()
        
        if user and check_password_hash(user['password_hash'], password):
            # Update last login
            with db_lock:
                conn = get_db_connection()
                conn.execute(
                    'UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?', (user['id'],)
                )
                conn.commit()
                conn.close()
            
            user_obj = User(user['id'], user['username'], user['email'], user['role'], 
                          user['full_name'], user['created_at'], user['is_active'],
                          user['instructor_approval_status'] if user['instructor_approval_status'] else 'approved',
                          user['approved_by'],
                          user['approved_at'])
            login_user(user_obj, remember=remember)
            flash(f'Welcome back, {user["full_name"]}!', 'success')
            
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password', 'error')
    
    return render_template('auth/login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        # Validate CSRF token
        csrf_token = request.form.get('csrf_token')
        if not validate_csrf_token(csrf_token):
            flash('Invalid security token. Please try again.', 'error')
            return render_template('auth/register.html')
            
        username = request.form['username'].strip()
        email = request.form['email'].lower().strip()
        password = request.form['password']
        confirm_password = request.form['confirm_password']
        full_name = request.form['full_name'].strip()
        role = request.form.get('role', 'student')
        
        # Handle instructor screenshot upload
        screenshot_filename = None
        if role == 'instructor' and 'instructor_screenshot' in request.files:
            screenshot = request.files['instructor_screenshot']
            if screenshot and screenshot.filename:
                # Create instructor_screenshots directory if it doesn't exist
                screenshots_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'instructor_screenshots')
                os.makedirs(screenshots_dir, exist_ok=True)
                
                # Validate file type
                allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
                file_ext = screenshot.filename.rsplit('.', 1)[1].lower() if '.' in screenshot.filename else ''
                
                if file_ext not in allowed_extensions:
                    flash('Invalid file type. Please upload PNG, JPG, JPEG, GIF, or WebP images only.', 'error')
                    return render_template('auth/register.html')
                
                # Validate file size (max 5MB)
                screenshot.seek(0, 2)  # Seek to end
                file_size = screenshot.tell()
                screenshot.seek(0)  # Reset seek position
                
                if file_size > 5 * 1024 * 1024:
                    flash('File size too large. Please upload images smaller than 5MB.', 'error')
                    return render_template('auth/register.html')
                
                # Generate secure filename and save
                filename = secure_filename(f"{username}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{screenshot.filename}")
                screenshot_path = os.path.join(screenshots_dir, filename)
                
                try:
                    screenshot.save(screenshot_path)
                    screenshot_filename = filename
                except Exception as e:
                    flash('Error saving screenshot. Please try again.', 'error')
                    return render_template('auth/register.html')
        
        # Validation
        if password != confirm_password:
            flash('Passwords do not match', 'error')
            return render_template('auth/register.html')
        
        if len(password) < 6:
            flash('Password must be at least 6 characters long', 'error')
            return render_template('auth/register.html')
        
        # Validate instructor screenshot
        if role == 'instructor' and not screenshot_filename:
            flash('Screenshot upload is required for instructor accounts', 'error')
            return render_template('auth/register.html')
        
        with db_lock:
            conn = get_db_connection()
            
            # Check if user already exists
            existing_user = conn.execute(
                'SELECT id FROM users WHERE email = ? OR username = ?', (email, username)
            ).fetchone()
            
            if existing_user:
                flash('User with this email or username already exists', 'error')
                conn.close()
                return render_template('auth/register.html')
            
            # Create new user
            password_hash = generate_password_hash(password)
            
            # Set instructor approval status based on role
            instructor_approval_status = 'pending' if role == 'instructor' else 'approved'
            
            conn.execute('''
                INSERT INTO users (username, email, password_hash, role, full_name, instructor_approval_status, instructor_screenshot)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (username, email, password_hash, role, full_name, instructor_approval_status, screenshot_filename))
            
            conn.commit()
            conn.close()
        
        # Flash appropriate message based on role
        if role == 'instructor':
            flash('Registration successful! Your instructor account is pending admin approval. You will be notified once approved.', 'info')
        else:
            flash('Registration successful! Please log in.', 'success')
        return redirect(url_for('login'))
    
    return render_template('auth/register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out successfully.', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    with db_lock:
        conn = get_db_connection()
        
        if current_user.is_admin():
            # Admin dashboard data
            stats = {
                'total_users': conn.execute('SELECT COUNT(*) FROM users WHERE is_active = 1').fetchone()[0],
                'total_students': conn.execute('SELECT COUNT(*) FROM users WHERE role = "student" AND is_active = 1').fetchone()[0],
                'total_courses': conn.execute('SELECT COUNT(*) FROM courses WHERE is_active = 1').fetchone()[0],
                'total_enrollments': conn.execute('SELECT COUNT(*) FROM enrollments WHERE status = "approved"').fetchone()[0],
                'pending_enrollments': conn.execute('SELECT COUNT(*) FROM enrollments WHERE status = "pending"').fetchone()[0],
                'pending_instructors': conn.execute('SELECT COUNT(*) FROM users WHERE role = "instructor" AND instructor_approval_status = "pending" AND is_active = 1').fetchone()[0],
                'total_instructors': conn.execute('SELECT COUNT(*) FROM users WHERE role = "instructor" AND is_active = 1').fetchone()[0]
            }
            
            # Recent enrollments
            recent_enrollments = conn.execute('''
                SELECT e.*, u.full_name as student_name, c.title as course_title
                FROM enrollments e
                JOIN users u ON e.student_id = u.id
                JOIN courses c ON e.course_id = c.id
                ORDER BY e.enrolled_at DESC
                LIMIT 10
            ''').fetchall()
            
            conn.close()
            return render_template('admin/dashboard.html', stats=stats, recent_enrollments=recent_enrollments)
        
        elif current_user.is_instructor() and current_user.is_instructor_approved():
            # Redirect to dedicated instructor dashboard only if instructor is approved
            conn.close()
            return redirect(url_for('instructor_dashboard'))
        
        else:
            # Student dashboard data - Use COALESCE to prioritize manual progress override
            my_enrollments_raw = conn.execute('''
                SELECT e.*, c.title, c.course_code, c.description, u.full_name as instructor_name,
                       COALESCE(e.manual_progress_override, e.progress_percentage) as display_progress
                FROM enrollments e
                JOIN courses c ON e.course_id = c.id
                JOIN users u ON c.instructor_id = u.id
                WHERE e.student_id = ?
                ORDER BY e.enrolled_at DESC
            ''', (current_user.id,)).fetchall()
            
            # Convert Row objects to dictionaries for JSON serialization
            my_enrollments = [dict(row) for row in my_enrollments_raw]
            
            # Available courses (not enrolled)
            available_courses = conn.execute('''
                SELECT c.*, u.full_name as instructor_name
                FROM courses c
                JOIN users u ON c.instructor_id = u.id
                LEFT JOIN enrollments e ON c.id = e.course_id AND e.student_id = ?
                WHERE c.is_active = 1 AND e.id IS NULL
                ORDER BY c.created_at DESC
            ''', (current_user.id,)).fetchall()
            
            conn.close()
            return render_template('student/dashboard.html', enrollments=my_enrollments, available_courses=available_courses)

@app.route('/update_profile', methods=['POST'])
@login_required
def update_profile():
    """Update user profile (name and picture)"""
    try:
        full_name = request.form.get('full_name', '').strip()
        
        if not full_name:
            flash('Name cannot be empty.', 'error')
            return redirect(url_for('dashboard'))
        
        with db_lock:
            conn = get_db_connection()
            
            # Update full name
            conn.execute('''
                UPDATE users SET full_name = ? WHERE id = ?
            ''', (full_name, current_user.id))
            
            # Handle profile picture upload
            if 'profile_picture' in request.files:
                file = request.files['profile_picture']
                if file and file.filename:
                    try:
                        # Validate file type
                        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
                        filename = secure_filename(file.filename)
                        file_ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
                        
                        if file_ext not in allowed_extensions:
                            logging.warning(f"Invalid file type: {file_ext}")
                            conn.close()
                            flash('Invalid file type. Please upload PNG, JPG, JPEG, GIF, or WebP images only.', 'error')
                            return redirect(url_for('dashboard'))
                        
                        # Create profile pictures directory
                        profile_pics_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'profile_pictures')
                        os.makedirs(profile_pics_dir, exist_ok=True)
                        
                        # Generate secure filename
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                        filename = f"{current_user.id}_{timestamp}_{filename}"
                        filepath = os.path.join(profile_pics_dir, filename)
                        
                        # Save file
                        file.save(filepath)
                        logging.info(f"Profile picture saved: {filepath}")
                        
                        # Update database with relative path
                        relative_path = os.path.join('profile_pictures', filename).replace('\\', '/')
                        conn.execute('''
                            UPDATE users SET profile_picture = ? WHERE id = ?
                        ''', (relative_path, current_user.id))
                    except Exception as file_error:
                        logging.error(f"Error processing file upload: {file_error}")
                        conn.close()
                        flash('Error processing file upload. Please try again.', 'error')
                        return redirect(url_for('dashboard'))
            
            conn.commit()
            
            # Reload the updated user data from database
            user_data = conn.execute('SELECT * FROM users WHERE id = ?', (current_user.id,)).fetchone()
            conn.close()
        
        # Update current_user object with new data
        current_user.full_name = full_name
        if user_data and 'profile_picture' in user_data.keys():
            current_user.profile_picture = user_data['profile_picture']
        
        flash('Profile updated successfully!', 'success')
        
    except Exception as e:
        logging.error(f"Error updating profile: {e}")
        flash('Error updating profile. Please try again.', 'error')
    
    return redirect(url_for('dashboard'))

# Admin instructor management routes
@app.route('/admin/instructors')
@admin_required
def admin_instructors():
    """Main instructor management page"""
    with db_lock:
        conn = get_db_connection()
        
        # Get all instructors with their approval status
        instructors_raw = conn.execute('''
            SELECT u.*, 
                   (SELECT full_name FROM users WHERE id = u.approved_by) as approved_by_name,
                   (SELECT COUNT(*) FROM courses WHERE instructor_id = u.id AND is_active = 1) as course_count
            FROM users u
            WHERE u.role = 'instructor' AND u.is_active = 1
            ORDER BY 
                CASE u.instructor_approval_status 
                    WHEN 'pending' THEN 1
                    WHEN 'rejected' THEN 2
                    WHEN 'approved' THEN 3
                END,
                u.created_at DESC
        ''').fetchall()
        
        # Convert Row objects to dictionaries for JSON serialization
        instructors = [dict(row) for row in instructors_raw]
        
        # Get counts for stats
        stats = {
            'total_instructors': len(instructors),
            'pending_instructors': len([i for i in instructors if i['instructor_approval_status'] == 'pending']),
            'approved_instructors': len([i for i in instructors if i['instructor_approval_status'] == 'approved']),
            'rejected_instructors': len([i for i in instructors if i['instructor_approval_status'] == 'rejected'])
        }
        
        conn.close()
    
    return render_template('admin/instructors.html', instructors=instructors, stats=stats)

@app.route('/admin/instructors/pending')
@admin_required
def admin_instructors_pending():
    """View pending instructor applications"""
    with db_lock:
        conn = get_db_connection()
        
        pending_instructors_raw = conn.execute('''
            SELECT u.*
            FROM users u
            WHERE u.role = 'instructor' 
              AND u.instructor_approval_status = 'pending' 
              AND u.is_active = 1
            ORDER BY u.created_at ASC
        ''').fetchall()
        
        # Convert Row objects to dictionaries for JSON serialization
        pending_instructors = [dict(row) for row in pending_instructors_raw]
        
        conn.close()
    
    return render_template('admin/instructors.html', 
                         instructors=pending_instructors, 
                         view_type='pending',
                         stats={'pending_instructors': len(pending_instructors)})

@app.route('/admin/instructors/approve/<int:instructor_id>', methods=['POST'])
@admin_required
def admin_approve_instructor(instructor_id):
    """Approve an instructor"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify instructor exists and is pending
        instructor = conn.execute('''
            SELECT * FROM users 
            WHERE id = ? AND role = 'instructor' AND instructor_approval_status = 'pending'
        ''', (instructor_id,)).fetchone()
        
        if not instructor:
            flash('Instructor not found or already processed.', 'error')
            conn.close()
            return redirect(url_for('admin_instructors'))
        
        # Approve the instructor
        conn.execute('''
            UPDATE users 
            SET instructor_approval_status = 'approved',
                approved_by = ?,
                approved_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (current_user.id, instructor_id))
        
        # Create notification for the instructor
        conn.execute('''
            INSERT INTO notifications (user_id, title, message, type)
            VALUES (?, ?, ?, ?)
        ''', (instructor_id, 
              'Instructor Account Approved',
              'Congratulations! Your instructor account has been approved. You can now create courses and access all instructor features.',
              'success'))
        
        conn.commit()
        conn.close()
    
    flash(f'Instructor {instructor["full_name"]} has been approved successfully.', 'success')
    return redirect(url_for('admin_instructors'))

@app.route('/admin/instructors/reject/<int:instructor_id>', methods=['POST'])
@admin_required
def admin_reject_instructor(instructor_id):
    """Reject an instructor"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify instructor exists and is pending
        instructor = conn.execute('''
            SELECT * FROM users 
            WHERE id = ? AND role = 'instructor' AND instructor_approval_status = 'pending'
        ''', (instructor_id,)).fetchone()
        
        if not instructor:
            flash('Instructor not found or already processed.', 'error')
            conn.close()
            return redirect(url_for('admin_instructors'))
        
        # Reject the instructor
        conn.execute('''
            UPDATE users 
            SET instructor_approval_status = 'rejected',
                approved_by = ?,
                approved_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (current_user.id, instructor_id))
        
        # Create notification for the instructor
        conn.execute('''
            INSERT INTO notifications (user_id, title, message, type)
            VALUES (?, ?, ?, ?)
        ''', (instructor_id,
              'Instructor Application Rejected',
              'We regret to inform you that your instructor application has been rejected. Please contact the administrator for more information.',
              'error'))
        
        conn.commit()
        conn.close()
    
    flash(f'Instructor {instructor["full_name"]} has been rejected.', 'warning')
    return redirect(url_for('admin_instructors'))

@app.route('/admin/instructors/create', methods=['POST'])
@admin_required
def admin_create_instructor():
    """Create a new instructor account"""
    username = request.form.get('username', '').strip().lower()
    email = request.form.get('email', '').strip().lower()
    full_name = request.form.get('full_name', '').strip()
    password = request.form.get('password', '').strip()
    
    if not all([username, email, full_name, password]):
        flash('All fields are required.', 'error')
        return redirect(url_for('admin_instructors'))
    
    with db_lock:
        conn = get_db_connection()
        
        # Check if user already exists
        existing = conn.execute(
            'SELECT id FROM users WHERE username = ? OR email = ?',
            (username, email)
        ).fetchone()
        
        if existing:
            flash('Username or email already exists.', 'error')
            conn.close()
            return redirect(url_for('admin_instructors'))
        
        try:
            conn.execute('''
                INSERT INTO users (username, email, password, full_name, role, instructor_approval_status, is_active)
                VALUES (?, ?, ?, ?, 'instructor', 'approved', 1)
            ''', (username, email, generate_password_hash(password), full_name))
            
            conn.commit()
            flash(f'Instructor {full_name} created successfully!', 'success')
        except Exception as e:
            conn.rollback()
            flash('Error creating instructor. Please try again.', 'error')
        finally:
            conn.close()
    
    return redirect(url_for('admin_instructors'))

@app.route('/admin/instructors/<int:instructor_id>/edit', methods=['POST'])
@admin_required
def admin_edit_instructor(instructor_id):
    """Edit instructor details"""
    with db_lock:
        conn = get_db_connection()
        
        instructor = conn.execute(
            'SELECT * FROM users WHERE id = ? AND role = "instructor"',
            (instructor_id,)
        ).fetchone()
        
        if not instructor:
            flash('Instructor not found.', 'error')
            conn.close()
            return redirect(url_for('admin_instructors'))
        
        email = request.form.get('email', '').strip().lower()
        full_name = request.form.get('full_name', '').strip()
        
        # Check if new email is already taken
        if email != instructor['email']:
            existing = conn.execute(
                'SELECT id FROM users WHERE email = ? AND id != ?',
                (email, instructor_id)
            ).fetchone()
            if existing:
                flash('Email already in use.', 'error')
                conn.close()
                return redirect(url_for('admin_instructors'))
        
        try:
            conn.execute('''
                UPDATE users 
                SET email = ?, full_name = ?
                WHERE id = ?
            ''', (email, full_name, instructor_id))
            
            conn.commit()
            flash(f'Instructor {full_name} updated successfully!', 'success')
        except Exception as e:
            conn.rollback()
            flash('Error updating instructor.', 'error')
        finally:
            conn.close()
    
    return redirect(url_for('admin_instructors'))

@app.route('/admin/instructors/<int:instructor_id>/delete', methods=['POST'])
@admin_required
def admin_delete_instructor(instructor_id):
    """Delete (deactivate) an instructor"""
    with db_lock:
        conn = get_db_connection()
        
        instructor = conn.execute(
            'SELECT * FROM users WHERE id = ? AND role = "instructor"',
            (instructor_id,)
        ).fetchone()
        
        if not instructor:
            flash('Instructor not found.', 'error')
            conn.close()
            return redirect(url_for('admin_instructors'))
        
        try:
            conn.execute(
                'UPDATE users SET is_active = 0 WHERE id = ?',
                (instructor_id,)
            )
            conn.commit()
            flash(f'Instructor {instructor["full_name"]} has been removed.', 'success')
        except Exception as e:
            conn.rollback()
            flash('Error removing instructor.', 'error')
        finally:
            conn.close()
    
    return redirect(url_for('admin_instructors'))

@app.route('/admin/instructors/<int:instructor_id>/toggle-block', methods=['POST'])
@admin_required
def admin_toggle_instructor_block(instructor_id):
    """Block or unblock an instructor"""
    with db_lock:
        conn = get_db_connection()
        
        instructor = conn.execute(
            'SELECT * FROM users WHERE id = ? AND role = "instructor"',
            (instructor_id,)
        ).fetchone()
        
        if not instructor:
            flash('Instructor not found.', 'error')
            conn.close()
            return redirect(url_for('admin_instructors'))
        
        new_status = 0 if instructor['is_active'] else 1
        action = 'unblocked' if new_status else 'blocked'
        
        try:
            conn.execute(
                'UPDATE users SET is_active = ? WHERE id = ?',
                (new_status, instructor_id)
            )
            conn.commit()
            flash(f'Instructor {instructor["full_name"]} has been {action}.', 'success')
        except Exception as e:
            conn.rollback()
            flash(f'Error {action} instructor.', 'error')
        finally:
            conn.close()
    
    return redirect(url_for('admin_instructors'))

@app.route('/admin/students')
@login_required
@admin_required
def admin_students():
    """View all students"""
    try:
        with db_lock:
            conn = get_db_connection()
            students_raw = conn.execute('''
                SELECT u.*, COUNT(DISTINCT e.id) as enrollment_count
                FROM users u
                LEFT JOIN enrollments e ON u.id = e.student_id AND e.status = 'approved'
                WHERE u.role = 'student'
                GROUP BY u.id
                ORDER BY u.created_at DESC
            ''').fetchall()
            
            # Convert Row objects to dictionaries for JSON serialization
            students = [dict(row) for row in students_raw]
            conn.close()
        
        return render_template('admin/students.html', students=students)
    except Exception as e:
        logging.error(f"Error fetching students: {e}")
        flash('Error loading students', 'error')
        return redirect(url_for('dashboard'))

@app.route('/admin/student/<int:student_id>/enrollments', methods=['GET'])
@admin_required
def admin_student_enrollments(student_id):
    """Get student's course enrollments for viewing"""
    try:
        with db_lock:
            conn = get_db_connection()
            enrollments_raw = conn.execute('''
                SELECT e.id, e.status, e.enrolled_at, e.progress_percentage,
                       c.title, c.course_code, u.full_name as instructor_name
                FROM enrollments e
                JOIN courses c ON e.course_id = c.id
                JOIN users u ON c.instructor_id = u.id
                WHERE e.student_id = ?
                ORDER BY e.enrolled_at DESC
            ''', (student_id,)).fetchall()
            
            enrollments = [dict(row) for row in enrollments_raw]
            conn.close()
        
        return jsonify({'success': True, 'enrollments': enrollments})
    except Exception as e:
        logging.error(f"Error fetching enrollments: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/admin/students/<int:student_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_student(student_id):
    """Delete a student account"""
    try:
        with db_lock:
            conn = get_db_connection()
            student = conn.execute('SELECT * FROM users WHERE id = ? AND role = "student"', (student_id,)).fetchone()
            
            if not student:
                conn.close()
                return jsonify({'success': False, 'error': 'Student not found'}), 404
            
            # Delete student's enrollments
            conn.execute('DELETE FROM enrollments WHERE student_id = ?', (student_id,))
            
            # Delete student's notes
            conn.execute('DELETE FROM student_notes WHERE student_id = ?', (student_id,))
            
            # Delete student account
            conn.execute('DELETE FROM users WHERE id = ?', (student_id,))
            conn.commit()
            conn.close()
            
        return jsonify({'success': True, 'message': 'Student deleted successfully'})
    except Exception as e:
        logging.error(f"Error deleting student: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/admin/students/<int:student_id>/toggle-block', methods=['POST'])
@login_required
@admin_required
def admin_toggle_student_block(student_id):
    """Toggle student block status"""
    try:
        with db_lock:
            conn = get_db_connection()
            student = conn.execute('SELECT * FROM users WHERE id = ? AND role = "student"', (student_id,)).fetchone()
            
            if not student:
                conn.close()
                return jsonify({'success': False, 'error': 'Student not found'}), 404
            
            # Toggle is_active status
            new_status = 0 if student['is_active'] else 1
            conn.execute('UPDATE users SET is_active = ? WHERE id = ?', (new_status, student_id))
            conn.commit()
            conn.close()
            
        return jsonify({'success': True, 'message': f'Student {"unblocked" if new_status else "blocked"} successfully'})
    except Exception as e:
        logging.error(f"Error toggling student block: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# INSTRUCTOR ROUTES
@app.route('/instructor/dashboard')
@instructor_required
def instructor_dashboard():
    """Enhanced instructor dashboard with course management"""
    with db_lock:
        conn = get_db_connection()
        
        # My courses
        my_courses = conn.execute('''
            SELECT c.*, 
                   COUNT(CASE WHEN e.status = 'approved' THEN 1 END) as approved_count,
                   COUNT(CASE WHEN e.status = 'pending' THEN 1 END) as pending_count,
                   COUNT(e.id) as total_enrollments
            FROM courses c
            LEFT JOIN enrollments e ON c.id = e.course_id
            WHERE c.instructor_id = ? AND c.is_active = 1
            GROUP BY c.id
            ORDER BY c.created_at DESC
        ''', (current_user.id,)).fetchall()
        
        # Pending enrollments for all my courses
        pending_enrollments = conn.execute('''
            SELECT e.*, u.full_name as student_name, u.email as student_email, 
                   c.title as course_title, c.course_code
            FROM enrollments e
            JOIN users u ON e.student_id = u.id
            JOIN courses c ON e.course_id = c.id
            WHERE c.instructor_id = ? AND e.status = 'pending'
            ORDER BY e.enrolled_at ASC
        ''', (current_user.id,)).fetchall()
        
        # Statistics
        stats = {
            'total_courses': len(my_courses),
            'total_students': sum([course['approved_count'] for course in my_courses]),
            'pending_enrollments': len(pending_enrollments),
            'active_courses': len([c for c in my_courses if c['approved_count'] > 0])
        }
        
        conn.close()
    
    return render_template('instructor/dashboard.html', 
                         courses=my_courses, 
                         pending_enrollments=pending_enrollments,
                         stats=stats)

@app.route('/instructor/courses/<int:course_id>/delete', methods=['POST'])
@instructor_required
def instructor_delete_course(course_id):
    """Delete a course"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ?
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        try:
            # Delete related data - only from tables that exist
            conn.execute('DELETE FROM enrollments WHERE course_id = ?', (course_id,))
            
            # Try to delete from optional tables if they exist
            try:
                conn.execute('DELETE FROM course_video_playlists WHERE course_id = ?', (course_id,))
            except:
                pass
            
            try:
                conn.execute('DELETE FROM course_resources WHERE course_id = ?', (course_id,))
            except:
                pass
            
            try:
                conn.execute('DELETE FROM course_meeting_links WHERE course_id = ?', (course_id,))
            except:
                pass
            
            try:
                conn.execute('DELETE FROM quizzes WHERE course_id = ?', (course_id,))
            except:
                pass
            
            try:
                conn.execute('DELETE FROM ai_notes WHERE course_id = ?', (course_id,))
            except:
                pass
            
            try:
                conn.execute('DELETE FROM discussion_forums WHERE course_id = ?', (course_id,))
            except:
                pass
            
            # Delete the course itself
            conn.execute('DELETE FROM courses WHERE id = ?', (course_id,))
            
            conn.commit()
            conn.close()
            
            flash(f'Course "{course["title"]}" has been deleted successfully.', 'success')
            
        except Exception as e:
            conn.rollback()
            conn.close()
            logging.error(f"Error deleting course: {e}")
            flash('Error deleting course. Please try again.', 'error')
    
    return redirect(url_for('instructor_courses'))


@app.route('/instructor/courses')
@instructor_required
def instructor_courses():
    """View and manage all instructor courses"""
    with db_lock:
        conn = get_db_connection()
        
        courses = conn.execute('''
            SELECT c.*, 
                   COUNT(CASE WHEN e.status = 'approved' THEN 1 END) as approved_count,
                   COUNT(CASE WHEN e.status = 'pending' THEN 1 END) as pending_count
            FROM courses c
            LEFT JOIN enrollments e ON c.id = e.course_id
            WHERE c.instructor_id = ? AND c.is_active = 1
            GROUP BY c.id
            ORDER BY c.created_at DESC
        ''', (current_user.id,)).fetchall()
        
        # Fetch AI Notes PDFs for each course
        courses_list = []
        for course in courses:
            course_dict = dict(course)
            ai_notes = conn.execute('''
                SELECT * FROM ai_notes 
                WHERE course_id = ? AND created_by = ? AND is_instructor_note = 1 AND pdf_path IS NOT NULL
                ORDER BY created_at DESC LIMIT 5
            ''', (course['id'], current_user.id)).fetchall()
            course_dict['ai_notes'] = ai_notes
            courses_list.append(course_dict)
        
        conn.close()
    
    return render_template('instructor/courses.html', courses=courses_list)

@app.route('/instructor/courses/create', methods=['GET', 'POST'])
@instructor_required
def instructor_create_course():
    """Create a new course with enrollment key"""
    if request.method == 'POST':
        # Get and validate required fields
        course_code = request.form.get('course_code', '').strip().upper()
        title = request.form.get('title', '').strip()
        enrollment_key = request.form.get('enrollment_key', '').strip()
        
        # Validate required fields
        if not course_code:
            flash('Course Code is required.', 'error')
            return render_template('instructor/create_course.html')
        if not title:
            flash('Course Title is required.', 'error')
            return render_template('instructor/create_course.html')
        if not enrollment_key:
            flash('Enrollment Key is required.', 'error')
            return render_template('instructor/create_course.html')
        
        # Get optional fields
        description = request.form.get('description', '').strip()
        syllabus = request.form.get('syllabus', '').strip()
        category = request.form.get('category', '').strip()
        max_students = int(request.form.get('max_students', 50))
        start_date = request.form.get('start_date') or None
        end_date = request.form.get('end_date') or None
        
        # Validate enrollment key
        if len(enrollment_key) < 6:
            flash('Enrollment key must be at least 6 characters long.', 'error')
            return render_template('instructor/create_course.html')
        
        # Hash the enrollment key for security
        enrollment_key_hash = generate_password_hash(enrollment_key)
        
        with db_lock:
            conn = get_db_connection()
            
            # Check if course code already exists
            existing_course = conn.execute(
                'SELECT id FROM courses WHERE course_code = ?', (course_code,)
            ).fetchone()
            
            if existing_course:
                flash('Course code already exists. Please choose a different one.', 'error')
                conn.close()
                return render_template('instructor/create_course.html')
            
            try:
                conn.execute('''
                    INSERT INTO courses (
                        course_code, title, description, syllabus, instructor_id, 
                        category, max_students, start_date, end_date, enrollment_key_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (course_code, title, description, syllabus, current_user.id,
                     category, max_students, start_date, end_date, enrollment_key_hash))
                
                conn.commit()
                conn.close()
                
                flash(f'Course "{title}" created successfully!', 'success')
                return redirect(url_for('instructor_courses'))
                
            except Exception as e:
                conn.rollback()
                conn.close()
                flash('Error creating course. Please try again.', 'error')
    
    return render_template('instructor/create_course.html')

@app.route('/instructor/courses/edit/<int:course_id>', methods=['GET', 'POST'])
@instructor_required
def instructor_edit_course(course_id):
    """Edit an existing course"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to current instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        if request.method == 'POST':
            title = request.form['title'].strip()
            description = request.form.get('description', '').strip()
            syllabus = request.form.get('syllabus', '').strip()
            category = request.form.get('category', '').strip()
            max_students = int(request.form.get('max_students', 50))
            start_date = request.form.get('start_date') or None
            end_date = request.form.get('end_date') or None
            
            # Handle enrollment key update
            enrollment_key = request.form.get('enrollment_key', '').strip()
            if enrollment_key:
                if len(enrollment_key) < 6:
                    flash('Enrollment key must be at least 6 characters long.', 'error')
                    conn.close()
                    return render_template('instructor/edit_course.html', course=course)
                enrollment_key_hash = generate_password_hash(enrollment_key)
            else:
                enrollment_key_hash = course['enrollment_key_hash']  # Keep existing
            
            try:
                conn.execute('''
                    UPDATE courses 
                    SET title = ?, description = ?, syllabus = ?, category = ?, 
                        max_students = ?, start_date = ?, end_date = ?, enrollment_key_hash = ?
                    WHERE id = ?
                ''', (title, description, syllabus, category, max_students, 
                     start_date, end_date, enrollment_key_hash, course_id))
                
                conn.commit()
                conn.close()
                
                flash(f'Course "{title}" updated successfully!', 'success')
                return redirect(url_for('instructor_courses'))
                
            except Exception as e:
                conn.rollback()
                conn.close()
                flash('Error updating course. Please try again.', 'error')
        
        conn.close()
    
    return render_template('instructor/edit_course.html', course=course)

@app.route('/instructor/edit-profile', methods=['GET', 'POST'])
@instructor_required
def edit_instructor_profile():
    """Edit instructor profile"""
    with db_lock:
        conn = get_db_connection()
        
        if request.method == 'POST':
            full_name = request.form.get('full_name', '').strip()
            email = request.form.get('email', '').strip()
            bio = request.form.get('bio', '').strip()
            
            if not full_name or not email:
                flash('Name and email are required.', 'error')
                user = current_user
                conn.close()
                return render_template('instructor/edit_profile.html', user=user)
            
            try:
                conn.execute('''
                    UPDATE users 
                    SET full_name = ?, email = ?, bio = ?
                    WHERE id = ?
                ''', (full_name, email, bio, current_user.id))
                
                conn.commit()
                
                # Update current_user session
                current_user.full_name = full_name
                current_user.email = email
                current_user.bio = bio
                
                flash('Profile updated successfully!', 'success')
                conn.close()
                return redirect(url_for('instructor_courses'))
                
            except Exception as e:
                conn.rollback()
                flash('Error updating profile. Please try again.', 'error')
                conn.close()
                return render_template('instructor/edit_profile.html', user=current_user)
        
        conn.close()
    
    return render_template('instructor/edit_profile.html', user=current_user)

@app.route('/instructor/enrollments')
@instructor_required 
def instructor_enrollments():
    """View all student enrollments for instructor's courses"""
    with db_lock:
        conn = get_db_connection()
        
        enrollments = conn.execute('''
            SELECT e.*, u.full_name as student_name, u.email as student_email,
                   c.title as course_title, c.course_code,
                   COALESCE(e.progress_percentage, 0) as progress_percentage
            FROM enrollments e
            JOIN users u ON e.student_id = u.id
            JOIN courses c ON e.course_id = c.id
            WHERE c.instructor_id = ?
            ORDER BY 
                CASE e.status 
                    WHEN 'pending' THEN 1
                    WHEN 'approved' THEN 2
                    WHEN 'rejected' THEN 3
                END,
                e.enrolled_at DESC
        ''', (current_user.id,)).fetchall()
        
        # Statistics
        stats = {
            'total_enrollments': len(enrollments),
            'pending_enrollments': len([e for e in enrollments if e['status'] == 'pending']),
            'approved_enrollments': len([e for e in enrollments if e['status'] == 'approved']),
            'rejected_enrollments': len([e for e in enrollments if e['status'] == 'rejected'])
        }
        
        conn.close()
    
    return render_template('instructor/enrollments.html', enrollments=enrollments, stats=stats)

@app.route('/instructor/enrollments/approve/<int:enrollment_id>', methods=['POST'])
@instructor_required
def instructor_approve_enrollment(enrollment_id):
    """Approve a student enrollment"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify enrollment belongs to instructor's course and is pending
        enrollment = conn.execute('''
            SELECT e.*, c.title as course_title, u.full_name as student_name
            FROM enrollments e
            JOIN courses c ON e.course_id = c.id
            JOIN users u ON e.student_id = u.id
            WHERE e.id = ? AND c.instructor_id = ? AND e.status = 'pending'
        ''', (enrollment_id, current_user.id)).fetchone()
        
        if not enrollment:
            flash('Enrollment not found or already processed.', 'error')
            conn.close()
            return redirect(url_for('instructor_enrollments'))
        
        try:
            # Approve the enrollment
            conn.execute('''
                UPDATE enrollments 
                SET status = 'approved', approved_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (enrollment_id,))
            
            # Create notification for the student
            conn.execute('''
                INSERT INTO notifications (user_id, title, message, type, related_id)
                VALUES (?, ?, ?, ?, ?)
            ''', (enrollment['student_id'],
                  'Enrollment Approved',
                  f'Great news! Your enrollment in "{enrollment["course_title"]}" has been approved. You now have full access to the course.',
                  'success',
                  enrollment['course_id']))
            
            conn.commit()
            conn.close()
            
            flash(f'Approved enrollment for {enrollment["student_name"]} in {enrollment["course_title"]}.', 'success')
            
        except Exception as e:
            conn.rollback()
            conn.close()
            flash('Error approving enrollment. Please try again.', 'error')
    
    return redirect(url_for('instructor_enrollments'))

@app.route('/instructor/enrollments/reject/<int:enrollment_id>', methods=['POST'])
@instructor_required
def instructor_reject_enrollment(enrollment_id):
    """Reject a student enrollment"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify enrollment belongs to instructor's course and is pending
        enrollment = conn.execute('''
            SELECT e.*, c.title as course_title, u.full_name as student_name
            FROM enrollments e
            JOIN courses c ON e.course_id = c.id
            JOIN users u ON e.student_id = u.id
            WHERE e.id = ? AND c.instructor_id = ? AND e.status = 'pending'
        ''', (enrollment_id, current_user.id)).fetchone()
        
        if not enrollment:
            flash('Enrollment not found or already processed.', 'error')
            conn.close()
            return redirect(url_for('instructor_enrollments'))
        
        try:
            # Reject the enrollment
            conn.execute('''
                UPDATE enrollments 
                SET status = 'rejected'
                WHERE id = ?
            ''', (enrollment_id,))
            
            # Create notification for the student
            conn.execute('''
                INSERT INTO notifications (user_id, title, message, type, related_id)
                VALUES (?, ?, ?, ?, ?)
            ''', (enrollment['student_id'],
                  'Enrollment Not Approved',
                  f'Your enrollment request for "{enrollment["course_title"]}" was not approved. Please contact the instructor for more information.',
                  'warning',
                  enrollment['course_id']))
            
            conn.commit()
            conn.close()
            
            flash(f'Rejected enrollment for {enrollment["student_name"]} in {enrollment["course_title"]}.', 'warning')
            
        except Exception as e:
            conn.rollback()
            conn.close()
            flash('Error rejecting enrollment. Please try again.', 'error')
    
    return redirect(url_for('instructor_enrollments'))


@app.route('/instructor/enrollments/block/<int:enrollment_id>', methods=['POST'])
@instructor_required
def instructor_block_student(enrollment_id):
    """Block a student from a course"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify enrollment belongs to instructor's course and is approved
        enrollment = conn.execute('''
            SELECT e.*, c.title as course_title, u.full_name as student_name
            FROM enrollments e
            JOIN courses c ON e.course_id = c.id
            JOIN users u ON e.student_id = u.id
            WHERE e.id = ? AND c.instructor_id = ? AND e.status = 'approved'
        ''', (enrollment_id, current_user.id)).fetchone()
        
        if not enrollment:
            flash('Enrollment not found or cannot be blocked.', 'error')
            conn.close()
            return redirect(url_for('instructor_enrollments'))
        
        try:
            # Block the enrollment
            conn.execute('''
                UPDATE enrollments 
                SET status = 'blocked'
                WHERE id = ?
            ''', (enrollment_id,))
            
            # Create notification for the student
            conn.execute('''
                INSERT INTO notifications (user_id, title, message, type, related_id)
                VALUES (?, ?, ?, ?, ?)
            ''', (enrollment['student_id'],
                  'Course Access Blocked',
                  f'Your access to "{enrollment["course_title"]}" has been blocked by the instructor.',
                  'warning',
                  enrollment['course_id']))
            
            conn.commit()
            conn.close()
            
            flash(f'Blocked {enrollment["student_name"]} from {enrollment["course_title"]}.', 'success')
            
        except Exception as e:
            conn.rollback()
            conn.close()
            flash('Error blocking student. Please try again.', 'error')
    
    return redirect(url_for('instructor_enrollments'))


@app.route('/instructor/enrollments/remove/<int:enrollment_id>', methods=['POST'])
@instructor_required
def instructor_remove_student(enrollment_id):
    """Remove a student from a course"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify enrollment belongs to instructor's course and is approved
        enrollment = conn.execute('''
            SELECT e.*, c.title as course_title, u.full_name as student_name
            FROM enrollments e
            JOIN courses c ON e.course_id = c.id
            JOIN users u ON e.student_id = u.id
            WHERE e.id = ? AND c.instructor_id = ? AND e.status = 'approved'
        ''', (enrollment_id, current_user.id)).fetchone()
        
        if not enrollment:
            flash('Enrollment not found or cannot be removed.', 'error')
            conn.close()
            return redirect(url_for('instructor_enrollments'))
        
        try:
            # Delete the enrollment
            conn.execute('DELETE FROM enrollments WHERE id = ?', (enrollment_id,))
            
            # Create notification for the student
            conn.execute('''
                INSERT INTO notifications (user_id, title, message, type, related_id)
                VALUES (?, ?, ?, ?, ?)
            ''', (enrollment['student_id'],
                  'Removed from Course',
                  f'You have been removed from "{enrollment["course_title"]}" by the instructor.',
                  'warning',
                  enrollment['course_id']))
            
            conn.commit()
            conn.close()
            
            flash(f'Removed {enrollment["student_name"]} from {enrollment["course_title"]}.', 'success')
            
        except Exception as e:
            conn.rollback()
            conn.close()
            flash('Error removing student. Please try again.', 'error')
    
    return redirect(url_for('instructor_enrollments'))


@app.route('/instructor/enrollments/unblock/<int:enrollment_id>', methods=['POST'])
@instructor_required
def instructor_unblock_student(enrollment_id):
    """Unblock a student from a course"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify enrollment belongs to instructor's course and is blocked
        enrollment = conn.execute('''
            SELECT e.*, c.title as course_title, u.full_name as student_name
            FROM enrollments e
            JOIN courses c ON e.course_id = c.id
            JOIN users u ON e.student_id = u.id
            WHERE e.id = ? AND c.instructor_id = ? AND e.status = 'blocked'
        ''', (enrollment_id, current_user.id)).fetchone()
        
        if not enrollment:
            flash('Enrollment not found or cannot be unblocked.', 'error')
            conn.close()
            return redirect(url_for('instructor_enrollments'))
        
        try:
            # Unblock the enrollment
            conn.execute('''
                UPDATE enrollments 
                SET status = 'approved'
                WHERE id = ?
            ''', (enrollment_id,))
            
            # Create notification for the student
            conn.execute('''
                INSERT INTO notifications (user_id, title, message, type, related_id)
                VALUES (?, ?, ?, ?, ?)
            ''', (enrollment['student_id'],
                  'Course Access Restored',
                  f'Your access to "{enrollment["course_title"]}" has been restored by the instructor.',
                  'success',
                  enrollment['course_id']))
            
            conn.commit()
            conn.close()
            
            flash(f'Unblocked {enrollment["student_name"]} for {enrollment["course_title"]}.', 'success')
            
        except Exception as e:
            conn.rollback()
            conn.close()
            flash('Error unblocking student. Please try again.', 'error')
    
    return redirect(url_for('instructor_enrollments'))

# STUDENT PROGRESS MANAGEMENT ROUTES
@app.route('/instructor/courses/<int:course_id>/students')
@instructor_required
def instructor_course_students(course_id):
    """View and manage student progress for a course"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        # Get all students enrolled in the course with their progress
        students = conn.execute('''
            SELECT 
                e.id as enrollment_id,
                e.student_id,
                u.full_name as student_name,
                u.email as student_email,
                e.status,
                e.enrolled_at,
                e.progress_percentage,
                e.manual_progress_override,
                COALESCE(e.manual_progress_override, e.progress_percentage) as display_progress
            FROM enrollments e
            JOIN users u ON e.student_id = u.id
            WHERE e.course_id = ? AND e.status = 'approved'
            ORDER BY u.full_name
        ''', (course_id,)).fetchall()
        
        conn.close()
    
    return render_template('instructor/course_students.html', course=course, students=students)

@app.route('/instructor/courses/<int:course_id>/set-progress-bulk', methods=['POST'])
@instructor_required
def instructor_set_progress_bulk(course_id):
    """Set manual progress override for all students in a course"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ?
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            conn.close()
            return jsonify({'success': False, 'error': 'Course not found or access denied'}), 403
        
        # Get progress value from request
        progress = request.form.get('progress')
        
        if progress is None or progress == '':
            conn.close()
            return jsonify({'success': False, 'error': 'Please enter a progress value'}), 400
        
        # Validate progress value
        try:
            progress = float(progress)
            if progress < 0 or progress > 100:
                conn.close()
                return jsonify({'success': False, 'error': 'Progress must be between 0 and 100'}), 400
        except ValueError:
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid progress value'}), 400
        
        # Get all students in the course
        try:
            students = conn.execute('''
                SELECT e.student_id, u.full_name
                FROM enrollments e
                JOIN users u ON e.student_id = u.id
                WHERE e.course_id = ? AND e.status = 'approved'
            ''', (course_id,)).fetchall()
            
            # Update progress for all students
            for student in students:
                conn.execute('''
                    UPDATE enrollments
                    SET manual_progress_override = ?, progress_percentage = ?
                    WHERE course_id = ? AND student_id = ?
                ''', (progress, progress, course_id, student['student_id']))
                
                # Send notification to each student
                send_notification(
                    student['student_id'],
                    'Course Progress Updated',
                    f'Your instructor has updated your progress in "{course["title"]}" to {progress:.0f}%',
                    'info',
                    course_id
                )
            
            conn.commit()
            conn.close()
            
            return jsonify({
                'success': True, 
                'message': f'Progress set to {progress:.0f}% for all {len(students)} students',
                'count': len(students)
            })
        except Exception as e:
            conn.rollback()
            conn.close()
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/instructor/courses/<int:course_id>/students/<int:student_id>/set-progress', methods=['POST'])
@instructor_required
def instructor_set_student_progress(course_id, student_id):
    """Set manual progress override for a student"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ?
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            conn.close()
            return jsonify({'success': False, 'error': 'Course not found or access denied'}), 403
        
        # Get progress value from request
        progress = request.form.get('progress')
        
        if progress is None or progress == '':
            # Clear manual override (reset to automatic calculation)
            try:
                conn.execute('''
                    UPDATE enrollments
                    SET manual_progress_override = NULL
                    WHERE course_id = ? AND student_id = ?
                ''', (course_id, student_id))
                conn.commit()
                
                # Immediately recalculate automatic progress to get current quiz-based progress
                auto_progress = update_student_progress(conn, student_id, course_id)
                
                # Get student info for notification
                student = conn.execute('SELECT full_name FROM users WHERE id = ?', (student_id,)).fetchone()
                
                # Send notification to student
                send_notification(
                    student_id,
                    'Course Progress Updated',
                    f'Your progress in "{course["title"]}" has been reset to automatic tracking ({auto_progress:.0f}%)',
                    'info',
                    course_id
                )
                
                conn.close()
                return jsonify({
                    'success': True, 
                    'message': f'Manual progress cleared. Automatic progress is {auto_progress:.0f}%',
                    'progress': auto_progress,
                    'is_manual': False
                })
            except Exception as e:
                conn.rollback()
                conn.close()
                return jsonify({'success': False, 'error': str(e)}), 500
        
        # Validate progress value
        try:
            progress = float(progress)
            if progress < 0 or progress > 100:
                conn.close()
                return jsonify({'success': False, 'error': 'Progress must be between 0 and 100'}), 400
        except ValueError:
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid progress value'}), 400
        
        # Set manual progress override
        try:
            conn.execute('''
                UPDATE enrollments
                SET manual_progress_override = ?, progress_percentage = ?
                WHERE course_id = ? AND student_id = ?
            ''', (progress, progress, course_id, student_id))
            
            # Get student info for notification
            student = conn.execute('SELECT full_name FROM users WHERE id = ?', (student_id,)).fetchone()
            
            # Send notification to student
            send_notification(
                student_id,
                'Course Progress Updated',
                f'Your instructor has updated your progress in "{course["title"]}" to {progress:.0f}%',
                'info',
                course_id
            )
            
            conn.commit()
            conn.close()
            
            return jsonify({
                'success': True, 
                'message': f'Progress set to {progress:.0f}% for {student["full_name"]}',
                'progress': progress,
                'is_manual': True
            })
        except Exception as e:
            conn.rollback()
            conn.close()
            return jsonify({'success': False, 'error': str(e)}), 500

# COURSE CONTENT MANAGEMENT ROUTES
@app.route('/instructor/courses/<int:course_id>/content')
@instructor_required
def instructor_course_content(course_id):
    """Manage course content - videos, notes, resources"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        # Get course resources
        resources = conn.execute('''
            SELECT * FROM course_resources 
            WHERE course_id = ?
            ORDER BY upload_date DESC
        ''', (course_id,)).fetchall()
        
        # Get meeting links
        meeting_links = conn.execute('''
            SELECT * FROM course_meeting_links 
            WHERE course_id = ? AND is_active = 1
            ORDER BY created_at DESC
        ''', (course_id,)).fetchall()
        
        # Get video playlist
        video_playlist = conn.execute('''
            SELECT * FROM course_video_playlists 
            WHERE course_id = ? AND is_active = 1
            ORDER BY order_index ASC
        ''', (course_id,)).fetchall()
        
        conn.close()
    
    return render_template('instructor/course_content.html', course=course, resources=resources, 
                         meeting_links=meeting_links, video_playlist=video_playlist)

@app.route('/instructor/courses/<int:course_id>/content/upload', methods=['POST'])
@instructor_required
def instructor_upload_content(course_id):
    """Upload course content - videos, notes, resources"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        content_type = request.form.get('content_type', 'document')
        
        if 'file' in request.files:
            file = request.files['file']
            if file and file.filename:
                filename = secure_filename(file.filename)
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], 'resources', f"{course_id}_{uuid.uuid4().hex}_{filename}")
                file.save(file_path)
                
                try:
                    conn.execute('''
                        INSERT INTO course_resources (course_id, title, description, file_path, file_type, uploaded_by)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (course_id, title, description, file_path, content_type, current_user.id))
                    
                    conn.commit()
                    flash(f'Content "{title}" uploaded successfully!', 'success')
                    
                except Exception as e:
                    conn.rollback()
                    flash('Error uploading content. Please try again.', 'error')
        else:
            flash('No file selected for upload.', 'error')
        
        conn.close()
    
    return redirect(url_for('instructor_course_content', course_id=course_id))

# MEETING LINKS MANAGEMENT ROUTES
@app.route('/instructor/courses/<int:course_id>/meeting-links/add', methods=['POST'])
@instructor_required
def instructor_add_meeting_link(course_id):
    """Add a meeting link to the course"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        title = request.form.get('title', '').strip()
        meeting_link = request.form.get('meeting_link', '').strip()
        description = request.form.get('description', '').strip()
        scheduled_time = request.form.get('scheduled_time') or None
        
        if not title or not meeting_link:
            flash('Meeting title and link are required.', 'error')
            conn.close()
            return redirect(url_for('instructor_course_content', course_id=course_id))
        
        try:
            conn.execute('''
                INSERT INTO course_meeting_links (course_id, title, meeting_link, description, scheduled_time, created_by)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (course_id, title, meeting_link, description, scheduled_time, current_user.id))
            
            conn.commit()
            flash(f'Meeting link "{title}" added successfully!', 'success')
            
        except Exception as e:
            conn.rollback()
            flash('Error adding meeting link. Please try again.', 'error')
        
        conn.close()
    
    return redirect(url_for('instructor_course_content', course_id=course_id))

@app.route('/instructor/courses/<int:course_id>/meeting-links/<int:link_id>/delete', methods=['POST'])
@instructor_required
def instructor_delete_meeting_link(course_id, link_id):
    """Delete a meeting link"""
    with db_lock:
        conn = get_db_connection()
        
        try:
            conn.execute('''
                DELETE FROM course_meeting_links 
                WHERE id = ? AND course_id = ? AND created_by = ?
            ''', (link_id, course_id, current_user.id))
            
            conn.commit()
            flash('Meeting link deleted successfully!', 'success')
            
        except Exception as e:
            conn.rollback()
            flash('Error deleting meeting link. Please try again.', 'error')
        
        conn.close()
    
    return redirect(url_for('instructor_course_content', course_id=course_id))

# VIDEO PLAYLIST MANAGEMENT ROUTES
@app.route('/instructor/courses/<int:course_id>/video-playlist/add', methods=['POST'])
@instructor_required
def instructor_add_video_playlist(course_id):
    """Add a video to the course playlist"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        title = request.form.get('title', '').strip()
        video_url = request.form.get('video_url', '').strip()
        description = request.form.get('description', '').strip()
        duration = request.form.get('duration', '').strip()
        thumbnail_url = request.form.get('thumbnail_url', '').strip()
        
        if not title or not video_url:
            flash('Video title and URL are required.', 'error')
            conn.close()
            return redirect(url_for('instructor_course_content', course_id=course_id))
        
        # Handle notes file upload with validation
        notes_file_path = None
        ALLOWED_NOTES_EXTENSIONS = {'pdf', 'doc', 'docx', 'txt', 'ppt', 'pptx', 'odt', 'rtf'}
        MAX_NOTES_SIZE = 50 * 1024 * 1024  # 50MB
        
        if 'notes_file' in request.files:
            notes_file = request.files['notes_file']
            if notes_file and notes_file.filename:
                filename = secure_filename(notes_file.filename)
                file_ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
                
                # Validate file extension
                if file_ext not in ALLOWED_NOTES_EXTENSIONS:
                    flash(f'Invalid file type. Allowed: {", ".join(ALLOWED_NOTES_EXTENSIONS)}', 'error')
                    conn.close()
                    return redirect(url_for('instructor_course_content', course_id=course_id))
                
                # Check file size
                notes_file.seek(0, 2)  # Seek to end
                file_size = notes_file.tell()
                notes_file.seek(0)  # Reset to beginning
                
                if file_size > MAX_NOTES_SIZE:
                    flash('Notes file too large. Maximum size is 50MB.', 'error')
                    conn.close()
                    return redirect(url_for('instructor_course_content', course_id=course_id))
                
                # Save the file
                notes_file_path = os.path.join(app.config['UPLOAD_FOLDER'], 'resources', f"notes_{course_id}_{uuid.uuid4().hex}_{filename}")
                os.makedirs(os.path.dirname(notes_file_path), exist_ok=True)
                notes_file.save(notes_file_path)
        
        try:
            # Get the next order index
            max_order = conn.execute('''
                SELECT MAX(order_index) FROM course_video_playlists 
                WHERE course_id = ?
            ''', (course_id,)).fetchone()[0]
            
            next_order = (max_order or 0) + 1
            
            conn.execute('''
                INSERT INTO course_video_playlists (course_id, title, video_url, description, duration, thumbnail_url, notes_file_path, order_index, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (course_id, title, video_url, description, duration, thumbnail_url, notes_file_path, next_order, current_user.id))
            
            conn.commit()
            flash(f'Video "{title}" added to playlist successfully!', 'success')
            
        except Exception as e:
            conn.rollback()
            flash('Error adding video to playlist. Please try again.', 'error')
        
        conn.close()
    
    return redirect(url_for('instructor_course_content', course_id=course_id))

@app.route('/download-notes/<int:video_id>')
@login_required
def download_video_notes(video_id):
    """Download notes for a video"""
    with db_lock:
        conn = get_db_connection()
        
        # Get video details
        video = conn.execute('''
            SELECT v.*, c.instructor_id 
            FROM course_video_playlists v
            JOIN courses c ON v.course_id = c.id
            WHERE v.id = ? AND v.is_active = 1
        ''', (video_id,)).fetchone()
        
        if not video or not video['notes_file_path']:
            conn.close()
            flash('Notes not found.', 'error')
            return redirect(url_for('dashboard'))
        
        # Check if user has access (instructor or enrolled student)
        if current_user.is_student():
            enrollment = conn.execute('''
                SELECT * FROM enrollments 
                WHERE student_id = ? AND course_id = ? AND status = 'approved'
            ''', (current_user.id, video['course_id'])).fetchone()
            
            if not enrollment:
                conn.close()
                flash('Access denied.', 'error')
                return redirect(url_for('dashboard'))
        elif current_user.is_instructor() and video['instructor_id'] != current_user.id:
            conn.close()
            flash('Access denied.', 'error')
            return redirect(url_for('dashboard'))
        
        stored_path = video['notes_file_path']
        conn.close()
        
        # Try to find the actual file - handle incorrect paths from different systems
        file_path = None
        filename = os.path.basename(stored_path)
        
        # Try multiple possible locations
        possible_paths = [
            stored_path,  # Original path
            os.path.join(app.config['UPLOAD_FOLDER'], 'resources', filename),  # Current system path
            os.path.join('sir_rafique', 'uploads', 'resources', filename),  # Relative path
        ]
        
        for path in possible_paths:
            if os.path.exists(path):
                file_path = path
                break
        
        if not file_path:
            logging.error(f"Notes file not found. Tried paths: {possible_paths}")
            flash('Notes file not found on server. Please contact your instructor.', 'error')
            return redirect(url_for('dashboard'))
        
        # Send the file
        try:
            directory = os.path.dirname(file_path)
            filename = os.path.basename(file_path)
            
            # Get original extension for proper MIME type
            file_ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else 'pdf'
            mime_types = {
                'pdf': 'application/pdf',
                'doc': 'application/msword',
                'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'txt': 'text/plain',
                'ppt': 'application/vnd.ms-powerpoint',
                'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
            }
            
            return send_from_directory(
                directory, 
                filename, 
                as_attachment=True,
                mimetype=mime_types.get(file_ext, 'application/octet-stream')
            )
        except Exception as e:
            logging.error(f"Error downloading notes: {e}")
            flash('Unable to download notes. Please try again.', 'error')
            return redirect(url_for('dashboard'))

@app.route('/instructor/courses/<int:course_id>/content/video/<int:video_id>/edit', methods=['POST'])
@instructor_required
def instructor_edit_video_playlist(course_id, video_id):
    """Edit a video in playlist"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify video belongs to instructor's course
        video = conn.execute('''
            SELECT v.* FROM course_video_playlists v
            JOIN courses c ON v.course_id = c.id
            WHERE v.id = ? AND v.course_id = ? AND c.instructor_id = ? AND v.is_active = 1
        ''', (video_id, course_id, current_user.id)).fetchone()
        
        if not video:
            conn.close()
            return jsonify({'success': False, 'message': 'Video not found or access denied.'}), 404
        
        try:
            title = request.form.get('title', '').strip()
            video_url = request.form.get('video_url', '').strip()
            duration = request.form.get('duration', '').strip()
            description = request.form.get('description', '').strip()
            
            if not title or not video_url:
                conn.close()
                return jsonify({'success': False, 'message': 'Title and URL are required.'}), 400
            
            # Update the video
            conn.execute('''
                UPDATE course_video_playlists
                SET title = ?, video_url = ?, duration = ?, description = ?
                WHERE id = ? AND course_id = ? AND is_active = 1
            ''', (title, video_url, duration or None, description or None, video_id, course_id))
            
            conn.commit()
            conn.close()
            
            return jsonify({'success': True, 'message': 'Video updated successfully!'}), 200
            
        except Exception as e:
            conn.rollback()
            conn.close()
            logging.error(f"Error updating video: {e}")
            return jsonify({'success': False, 'message': 'Error updating video. Please try again.'}), 500

@app.route('/instructor/courses/<int:course_id>/video-playlist/<int:video_id>/delete', methods=['POST'])
@instructor_required
def instructor_delete_video_playlist(course_id, video_id):
    """Delete a video from playlist"""
    with db_lock:
        conn = get_db_connection()
        
        try:
            conn.execute('''
                DELETE FROM course_video_playlists 
                WHERE id = ? AND course_id = ? AND created_by = ?
            ''', (video_id, course_id, current_user.id))
            
            conn.commit()
            flash('Video deleted from playlist successfully!', 'success')
            
        except Exception as e:
            conn.rollback()
            flash('Error deleting video. Please try again.', 'error')
        
        conn.close()
    
    return redirect(url_for('instructor_course_content', course_id=course_id))

# STUDENT VIDEO PLAYLIST ROUTES
@app.route('/student/my-playlist')
@login_required
def student_my_playlist():
    """View student's personal video playlist"""
    if not current_user.is_student():
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    
    with db_lock:
        conn = get_db_connection()
        rows = conn.execute('''
            SELECT * FROM student_video_playlists 
            WHERE student_id = ? AND is_active = 1
            ORDER BY order_index ASC
        ''', (current_user.id,)).fetchall()
        # Convert Row objects to dictionaries for JSON serialization
        videos = [dict(row) for row in rows]
        conn.close()
    
    return render_template('student/my_playlist.html', videos=videos)

@app.route('/student/playlist/add', methods=['POST'])
@login_required
def student_add_playlist_video():
    """Add video to student playlist"""
    if not current_user.is_student():
        return jsonify({'success': False, 'message': 'Access denied.'}), 403
    
    with db_lock:
        conn = get_db_connection()
        
        title = request.form.get('title', '').strip()
        video_url = request.form.get('video_url', '').strip()
        description = request.form.get('description', '').strip()
        duration = request.form.get('duration', '').strip()
        
        if not title or not video_url:
            conn.close()
            return jsonify({'success': False, 'message': 'Title and URL are required.'}), 400
        
        try:
            conn.execute('''
                INSERT INTO student_video_playlists 
                (student_id, title, video_url, description, duration, order_index)
                VALUES (?, ?, ?, ?, ?, (SELECT COUNT(*) FROM student_video_playlists WHERE student_id = ?))
            ''', (current_user.id, title, video_url, description or None, duration or None, current_user.id))
            
            conn.commit()
            conn.close()
            
            return jsonify({'success': True, 'message': 'Video added to playlist!'}), 200
        except Exception as e:
            conn.rollback()
            conn.close()
            logging.error(f"Error adding playlist video: {e}")
            return jsonify({'success': False, 'message': 'Error adding video. Please try again.'}), 500

@app.route('/student/playlist/<int:video_id>/edit', methods=['POST'])
@login_required
def student_edit_playlist_video(video_id):
    """Edit student playlist video"""
    if not current_user.is_student():
        return jsonify({'success': False, 'message': 'Access denied.'}), 403
    
    with db_lock:
        conn = get_db_connection()
        
        video = conn.execute('''
            SELECT * FROM student_video_playlists 
            WHERE id = ? AND student_id = ? AND is_active = 1
        ''', (video_id, current_user.id)).fetchone()
        
        if not video:
            conn.close()
            return jsonify({'success': False, 'message': 'Video not found.'}), 404
        
        title = request.form.get('title', '').strip()
        video_url = request.form.get('video_url', '').strip()
        description = request.form.get('description', '').strip()
        duration = request.form.get('duration', '').strip()
        
        if not title or not video_url:
            conn.close()
            return jsonify({'success': False, 'message': 'Title and URL are required.'}), 400
        
        try:
            conn.execute('''
                UPDATE student_video_playlists
                SET title = ?, video_url = ?, description = ?, duration = ?
                WHERE id = ? AND student_id = ? AND is_active = 1
            ''', (title, video_url, description or None, duration or None, video_id, current_user.id))
            
            conn.commit()
            conn.close()
            
            return jsonify({'success': True, 'message': 'Video updated successfully!'}), 200
        except Exception as e:
            conn.rollback()
            conn.close()
            logging.error(f"Error updating playlist video: {e}")
            return jsonify({'success': False, 'message': 'Error updating video.'}), 500

@app.route('/student/playlist/<int:video_id>/delete', methods=['POST'])
@login_required
def student_delete_playlist_video(video_id):
    """Delete video from student playlist"""
    if not current_user.is_student():
        return jsonify({'success': False, 'message': 'Access denied.'}), 403
    
    with db_lock:
        conn = get_db_connection()
        
        try:
            conn.execute('''
                DELETE FROM student_video_playlists 
                WHERE id = ? AND student_id = ?
            ''', (video_id, current_user.id))
            
            conn.commit()
            conn.close()
            
            return jsonify({'success': True, 'message': 'Video deleted!'}), 200
        except Exception as e:
            conn.rollback()
            conn.close()
            logging.error(f"Error deleting playlist video: {e}")
            return jsonify({'success': False, 'message': 'Error deleting video.'}), 500

@app.route('/generate-transcript/<int:video_id>', methods=['POST'])
@login_required
def generate_video_transcript(video_id):
    """Generate AI transcript for a video and save as PDF with watermark"""
    try:
        from utils.pdf_generator import generate_transcript_pdf
    except ImportError:
        logging.error("PDF generator not available")
        return jsonify({'success': False, 'message': 'PDF generation service not available. Please check system configuration.'}), 500
    
    with db_lock:
        conn = get_db_connection()
        
        # Get video details
        video = conn.execute('''
            SELECT v.*, c.instructor_id, c.title as course_title, c.course_code
            FROM course_video_playlists v
            JOIN courses c ON v.course_id = c.id
            WHERE v.id = ? AND v.is_active = 1
        ''', (video_id,)).fetchone()
        
        if not video:
            conn.close()
            return jsonify({'success': False, 'message': 'Video not found.'}), 404
        
        # Check if user has access (instructor or enrolled student)
        if current_user.is_student():
            enrollment = conn.execute('''
                SELECT * FROM enrollments 
                WHERE student_id = ? AND course_id = ? AND status = 'approved'
            ''', (current_user.id, video['course_id'])).fetchone()
            
            if not enrollment:
                conn.close()
                return jsonify({'success': False, 'message': 'Access denied.'}), 403
        elif current_user.is_instructor() and video['instructor_id'] != current_user.id:
            conn.close()
            return jsonify({'success': False, 'message': 'Access denied.'}), 403
        
        try:
            # Generate transcript using Gemini AI from actual YouTube video
            transcript_text = gemini_ai.generate_video_transcript(
                video['title'],
                video['description'] or '',
                video['duration'] or '',
                video['video_url'] or ''
            )
            
            if not transcript_text or len(transcript_text) < 10:
                conn.close()
                return jsonify({'success': False, 'message': 'Failed to generate transcript. Please check your Gemini API key.'}), 500
            
            # Generate PDF filename
            safe_filename = secure_filename(video['title'])
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            pdf_filename = f"transcript_{video_id}_{timestamp}.pdf"
            transcript_folder = os.path.join(app.config['UPLOAD_FOLDER'], 'transcripts')
            os.makedirs(transcript_folder, exist_ok=True)
            pdf_path = os.path.join(transcript_folder, pdf_filename)
            
            # Generate PDF with watermark
            success = generate_transcript_pdf(
                transcript_text=transcript_text,
                video_title=video['title'],
                course_name=f"{video['course_code']} - {video['course_title']}",
                student_name=current_user.full_name,
                output_path=pdf_path
            )
            
            if success:
                # Save relative path to database (not absolute)
                relative_path = os.path.join('sir_rafique', 'uploads', 'transcripts', pdf_filename)
                
                # Update database with transcript path
                conn.execute('''
                    UPDATE course_video_playlists 
                    SET transcript_file_path = ?
                    WHERE id = ?
                ''', (relative_path, video_id))
                conn.commit()
                conn.close()
                
                logging.info(f"Transcript generated successfully for video {video_id}")
                return jsonify({
                    'success': True,
                    'message': 'Transcript generated successfully!',
                    'download_url': url_for('download_video_transcript', video_id=video_id)
                })
            else:
                conn.close()
                return jsonify({'success': False, 'message': 'Error generating PDF. Please try again.'}), 500
                
        except ValueError as ve:
            conn.close()
            logging.error(f"Gemini API Key Error: {ve}")
            return jsonify({'success': False, 'message': 'Gemini API is not configured. Please set GEMINI_API_KEY environment variable.'}), 500
        except Exception as e:
            conn.close()
            logging.error(f"Error generating transcript: {e}")
            return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/generate-student-notes', methods=['POST'])
@login_required
def generate_student_notes_route():
    """Generate AI-enhanced notes from student input and save as PDF with LearnNest watermark"""
    try:
        from utils.pdf_generator import generate_transcript_pdf
    except ImportError:
        logging.error("PDF generator not available")
        return jsonify({'success': False, 'message': 'PDF generation service not available.'}), 500
    
    try:
        data = request.get_json()
        student_notes_input = data.get('topic', '').strip()
        course_id = data.get('course_id')
        add_watermark = data.get('add_watermark', True)  # Default to True
        
        if not student_notes_input or len(student_notes_input) < 3:
            return jsonify({'success': False, 'message': 'Please enter a topic.'}), 400
        
        with db_lock:
            conn = get_db_connection()
            
            # Get course details
            course = conn.execute('''
                SELECT * FROM courses WHERE id = ?
            ''', (course_id,)).fetchone()
            
            if not course:
                conn.close()
                return jsonify({'success': False, 'message': 'Course not found.'}), 404
            
            # Check student has access
            if current_user.is_student():
                enrollment = conn.execute('''
                    SELECT * FROM enrollments 
                    WHERE student_id = ? AND course_id = ? AND status = 'approved'
                ''', (current_user.id, course_id)).fetchone()
                if not enrollment:
                    conn.close()
                    return jsonify({'success': False, 'message': 'Access denied.'}), 403
            
            # Generate enhanced notes using Gemini AI
            enhanced_notes = gemini_ai.generate_student_notes(
                student_notes_input,
                course['title'],
                course['course_code']
            )
            
            if not enhanced_notes or len(enhanced_notes) < 10:
                conn.close()
                return jsonify({'success': False, 'message': 'Failed to enhance notes. Please try again.'}), 500
            
            # Generate PDF filename
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            pdf_filename = f"student_notes_{current_user.id}_{timestamp}.pdf"
            notes_folder = os.path.join(app.config['UPLOAD_FOLDER'], 'student_notes')
            os.makedirs(notes_folder, exist_ok=True)
            pdf_path = os.path.join(notes_folder, pdf_filename)
            
            # Generate PDF with optional watermark
            success = generate_transcript_pdf(
                transcript_text=enhanced_notes,
                video_title=f"Study Notes - {course['title']}",
                course_name=f"{course['course_code']} - {course['title']}",
                student_name=current_user.full_name,
                output_path=pdf_path,
                add_watermark=add_watermark
            )
            
            if success:
                # Save to database
                conn.execute('''
                    INSERT INTO student_notes (student_id, course_id, original_input, enhanced_notes, file_path, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    current_user.id,
                    course_id,
                    student_notes_input,
                    enhanced_notes,
                    os.path.join('sir_rafique', 'uploads', 'student_notes', pdf_filename),
                    datetime.now()
                ))
                conn.commit()
                
                # Get the inserted note ID
                note_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                conn.close()
                
                # Send notification to student
                socketio.emit('notification', {
                    'title': '📚 Study Notes Generated!',
                    'message': f'Your AI-generated study notes for "{student_notes_input}" are ready!',
                    'type': 'success',
                    'icon': 'fa-book'
                }, to=f'user_{current_user.id}')
                
                logging.info(f"Student notes generated for student {current_user.id}")
                return jsonify({
                    'success': True,
                    'message': 'Enhanced study notes generated successfully!',
                    'note_id': note_id,
                    'enhanced_notes': enhanced_notes,
                    'download_url': url_for('download_student_notes', note_id=note_id)
                })
            else:
                conn.close()
                return jsonify({'success': False, 'message': 'Error generating PDF.'}), 500
                
    except ValueError as ve:
        logging.error(f"Gemini API Key Error: {ve}")
        return jsonify({'success': False, 'message': 'Gemini API not configured.'}), 500
    except Exception as e:
        logging.error(f"Error generating student notes: {e}")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/download-student-notes/<int:note_id>')
@login_required
def download_student_notes(note_id):
    """Download student's enhanced notes PDF"""
    try:
        with db_lock:
            conn = get_db_connection()
            note = conn.execute('''
                SELECT * FROM student_notes WHERE id = ? AND student_id = ?
            ''', (note_id, current_user.id)).fetchone()
            conn.close()
        
        if not note:
            logging.warning(f"Note {note_id} not found for student {current_user.id}")
            return jsonify({'error': 'Note not found'}), 404
        
        stored_path = note['file_path']
        filename = os.path.basename(stored_path)
        
        # Try multiple possible locations
        possible_paths = [
            stored_path,  # Original path as stored
            os.path.join(app.config['UPLOAD_FOLDER'], filename),  # In uploads folder
            os.path.join(app.config['UPLOAD_FOLDER'], 'student_notes', filename),  # In student_notes subfolder
            os.path.join('sir_rafique', 'uploads', filename),  # Relative path from project root
            os.path.join('sir_rafique', 'uploads', 'student_notes', filename),  # With subfolder
        ]
        
        file_path = None
        for path in possible_paths:
            if os.path.exists(path):
                file_path = path
                logging.info(f"Found notes file at: {path}")
                break
        
        if not file_path:
            logging.error(f"Student notes file not found. Note ID: {note_id}, Stored path: {stored_path}, Tried paths: {possible_paths}")
            return jsonify({'error': 'File not found on server'}), 404
        
        # Send the file
        try:
            directory = os.path.dirname(file_path)
            filename = os.path.basename(file_path)
            return send_file(file_path, as_attachment=True, download_name=f"study_notes_{note_id}.pdf")
        except Exception as send_error:
            logging.error(f"Error sending file: {send_error}")
            return jsonify({'error': 'Error sending file'}), 500
            
    except Exception as e:
        logging.error(f"Error downloading student notes: {e}")
        return jsonify({'error': 'Download error'}), 500

@app.route('/api/ai-assistant', methods=['POST'])
@login_required
def ai_assistant():
    """AI Study Assistant - Answer student questions instantly using Gemini AI with visual generation"""
    try:
        data = request.get_json()
        question = data.get('question', '').strip()
        
        if not question or len(question) < 3:
            return jsonify({'success': False, 'message': 'Please enter a valid question.'}), 400
        
        try:
            # Use Gemini AI to answer the question with visual capability
            result = gemini_ai.answer_student_question(question)
            
            # Handle both dict and string responses for backward compatibility
            if isinstance(result, dict):
                answer_text = result.get('answer', '')
                needs_visual = result.get('needs_visual', False)
                visual_prompt = result.get('visual_prompt', None)
            else:
                answer_text = result
                needs_visual = False
                visual_prompt = None
            
            if answer_text and "error" not in answer_text.lower():
                response_data = {
                    'success': True,
                    'answer': answer_text,
                    'has_visual': needs_visual
                }
                
                # If visual is needed, generate it using Gemini's Imagen
                if needs_visual and visual_prompt:
                    try:
                        from google import genai
                        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
                        
                        # Generate image using Imagen 3
                        image_response = client.models.generate_images(
                            model='imagen-3.0-generate-001',
                            prompt=visual_prompt,
                            config=types.GenerateImagesConfig(
                                number_of_images=1,
                                aspect_ratio='1:1',
                                safety_filter_level='block_some',
                                person_generation='allow_adult'
                            )
                        )
                        
                        if image_response and image_response.generated_images:
                            # Save the generated image
                            import base64
                            image_data = image_response.generated_images[0].image.image_bytes
                            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                            filename = f"ai_visual_{current_user.id}_{timestamp}.png"
                            visual_folder = os.path.join(app.config['UPLOAD_FOLDER'], 'ai_visuals')
                            os.makedirs(visual_folder, exist_ok=True)
                            image_path = os.path.join(visual_folder, filename)
                            
                            with open(image_path, 'wb') as f:
                                f.write(image_data)
                            
                            response_data['visual_url'] = f"/uploads/ai_visuals/{filename}"
                            logging.info(f"Generated visual for question: {question[:50]}...")
                    except Exception as img_error:
                        logging.warning(f"Could not generate visual: {img_error}")
                        # Continue without visual if generation fails
                
                logging.info(f"AI Assistant answered question for user {current_user.id}")
                return jsonify(response_data)
            else:
                return jsonify({
                    'success': False,
                    'message': 'Unable to generate answer at this time. Please try again.'
                }), 500
                
        except Exception as e:
            logging.error(f"Error in AI Assistant: {e}")
            return jsonify({
                'success': False,
                'message': 'Error processing your question. Please try again.'
            }), 500
            
    except Exception as e:
        logging.error(f"AI Assistant request error: {e}")
        return jsonify({'success': False, 'message': 'Invalid request.'}), 400

@app.route('/generate-notes/<int:video_id>', methods=['POST'])
@login_required
def generate_video_notes_route(video_id):
    """Generate AI study notes for a video and save as PDF"""
    try:
        from utils.pdf_generator import generate_transcript_pdf
    except ImportError:
        logging.error("PDF generator not available")
        return jsonify({'success': False, 'message': 'PDF generation service not available. Please check system configuration.'}), 500
    
    with db_lock:
        conn = get_db_connection()
        
        # Get video details
        video = conn.execute('''
            SELECT v.*, c.instructor_id, c.title as course_title, c.course_code
            FROM course_video_playlists v
            JOIN courses c ON v.course_id = c.id
            WHERE v.id = ? AND v.is_active = 1
        ''', (video_id,)).fetchone()
        
        if not video:
            conn.close()
            return jsonify({'success': False, 'message': 'Video not found.'}), 404
        
        # Check if user has access (instructor or enrolled student)
        if current_user.is_student():
            enrollment = conn.execute('''
                SELECT * FROM enrollments 
                WHERE student_id = ? AND course_id = ? AND status = 'approved'
            ''', (current_user.id, video['course_id'])).fetchone()
            
            if not enrollment:
                conn.close()
                return jsonify({'success': False, 'message': 'Access denied.'}), 403
        elif current_user.is_instructor() and video['instructor_id'] != current_user.id:
            conn.close()
            return jsonify({'success': False, 'message': 'Access denied.'}), 403
        
        try:
            # Generate AI notes using Gemini AI
            notes_text = gemini_ai.generate_video_notes(
                video['title'],
                video['description'] or '',
                video['duration'] or '',
                video['video_url'] or ''
            )
            
            if not notes_text or len(notes_text) < 10:
                conn.close()
                return jsonify({'success': False, 'message': 'Failed to generate notes. Please check your Gemini API key.'}), 500
            
            # Generate PDF filename
            safe_filename = secure_filename(video['title'])
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            pdf_filename = f"ai_notes_{video_id}_{timestamp}.pdf"
            notes_folder = os.path.join(app.config['UPLOAD_FOLDER'], 'resources')
            os.makedirs(notes_folder, exist_ok=True)
            pdf_path = os.path.join(notes_folder, pdf_filename)
            
            # Generate PDF with watermark (reuse transcript PDF function with different title)
            success = generate_transcript_pdf(
                transcript_text=notes_text,
                video_title=f"Study Notes: {video['title']}",
                course_name=f"{video['course_code']} - {video['course_title']}",
                student_name=current_user.full_name,
                output_path=pdf_path
            )
            
            if success:
                # Save relative path to database (not absolute)
                relative_path = os.path.join('sir_rafique', 'uploads', 'resources', pdf_filename)
                
                # Update database with AI notes path
                conn.execute('''
                    UPDATE course_video_playlists 
                    SET notes_file_path = ?
                    WHERE id = ?
                ''', (relative_path, video_id))
                conn.commit()
                conn.close()
                
                logging.info(f"AI notes generated successfully for video {video_id}")
                return jsonify({
                    'success': True,
                    'message': 'AI Study Notes generated successfully!',
                    'download_url': url_for('download_video_notes', video_id=video_id)
                })
            else:
                conn.close()
                return jsonify({'success': False, 'message': 'Error generating PDF. Please try again.'}), 500
                
        except ValueError as ve:
            conn.close()
            logging.error(f"Gemini API Key Error: {ve}")
            return jsonify({'success': False, 'message': 'Gemini API is not configured. Please set GEMINI_API_KEY environment variable.'}), 500
        except Exception as e:
            conn.close()
            logging.error(f"Error generating notes: {e}")
            return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/download-transcript/<int:video_id>')
@login_required
def download_video_transcript(video_id):
    """Download AI-generated transcript PDF for a video"""
    with db_lock:
        conn = get_db_connection()
        
        # Get video details
        video = conn.execute('''
            SELECT v.*, c.instructor_id 
            FROM course_video_playlists v
            JOIN courses c ON v.course_id = c.id
            WHERE v.id = ? AND v.is_active = 1
        ''', (video_id,)).fetchone()
        
        if not video or not video['transcript_file_path']:
            conn.close()
            flash('Transcript not found. Please generate it first.', 'error')
            return redirect(url_for('dashboard'))
        
        # Check if user has access (instructor or enrolled student)
        if current_user.is_student():
            enrollment = conn.execute('''
                SELECT * FROM enrollments 
                WHERE student_id = ? AND course_id = ? AND status = 'approved'
            ''', (current_user.id, video['course_id'])).fetchone()
            
            if not enrollment:
                conn.close()
                flash('Access denied.', 'error')
                return redirect(url_for('dashboard'))
        elif current_user.is_instructor() and video['instructor_id'] != current_user.id:
            conn.close()
            flash('Access denied.', 'error')
            return redirect(url_for('dashboard'))
        
        stored_path = video['transcript_file_path']
        conn.close()
        
        # Try to find the actual file - handle incorrect paths from different systems
        file_path = None
        filename = os.path.basename(stored_path)
        
        # Try multiple possible locations
        possible_paths = [
            stored_path,  # Original path
            os.path.join(app.config['UPLOAD_FOLDER'], 'transcripts', filename),  # Current system path
            os.path.join('sir_rafique', 'uploads', 'transcripts', filename),  # Relative path
        ]
        
        for path in possible_paths:
            if os.path.exists(path):
                file_path = path
                break
        
        if not file_path:
            logging.error(f"Transcript file not found. Tried paths: {possible_paths}")
            flash('Transcript file not found. Please generate it again.', 'error')
            return redirect(url_for('dashboard'))
        
        # Send the transcript PDF file
        try:
            directory = os.path.dirname(file_path)
            filename = os.path.basename(file_path)
            
            # Create a clean download filename
            safe_title = "".join(c for c in video['title'] if c.isalnum() or c in (' ', '-', '_')).rstrip()
            download_filename = f"transcript_{safe_title}.pdf"
            
            return send_from_directory(
                directory, 
                filename, 
                as_attachment=True, 
                download_name=download_filename,
                mimetype='application/pdf'
            )
        except Exception as e:
            logging.error(f"Error downloading transcript: {e}")
            flash(f'Unable to download transcript. Please try again.', 'error')
            return redirect(url_for('dashboard'))

@app.route('/download-video/<int:video_id>')
@login_required
def download_video(video_id):
    """Download YouTube video for a course using yt-dlp"""
    import subprocess
    import tempfile
    
    with db_lock:
        conn = get_db_connection()
        
        # Get video details
        video = conn.execute('''
            SELECT v.*, c.instructor_id 
            FROM course_video_playlists v
            JOIN courses c ON v.course_id = c.id
            WHERE v.id = ? AND v.is_active = 1
        ''', (video_id,)).fetchone()
        
        if not video:
            conn.close()
            flash('Video not found.', 'error')
            return redirect(url_for('dashboard'))
        
        # Check if user has access (instructor or enrolled student)
        if current_user.is_student():
            enrollment = conn.execute('''
                SELECT * FROM enrollments 
                WHERE student_id = ? AND course_id = ? AND status = 'approved'
            ''', (current_user.id, video['course_id'])).fetchone()
            
            if not enrollment:
                conn.close()
                flash('Access denied.', 'error')
                return redirect(url_for('dashboard'))
        elif current_user.is_instructor() and video['instructor_id'] != current_user.id:
            conn.close()
            flash('Access denied.', 'error')
            return redirect(url_for('dashboard'))
        
        video_url = video['video_url']
        conn.close()
        
        # Render download page with embedded downloader
        return render_template('student/video_download.html', video=video)

# VIDEO DOWNLOADER ROUTES (PyTubeFix Integration)
@app.route('/video-downloader')
@login_required
def video_downloader_home():
    """Main video downloader page"""
    return render_template('student/video_download.html', video=None)

def clean_youtube_url(link: str) -> str:
    """Cleans YouTube URL by removing tracking parameters while preserving the video ID."""
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    
    parsed = urlparse(link)
    query_params = parse_qs(parsed.query)
    
    # Keep only the 'v' parameter (video ID) if it exists
    if 'v' in query_params:
        cleaned_query = urlencode({'v': query_params['v'][0]})
        cleaned_url = urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            cleaned_query,
            ''  # Remove fragment
        ))
        return cleaned_url
    
    # If no 'v' parameter, return original (might be a youtu.be short link)
    return link

@app.route('/submit', methods=['GET', 'POST'])
@login_required
def submit_video_download():
    """Download YouTube video using PyTubeFix"""
    try:
        # Get link from either query parameter (GET) or form data (POST)
        link = request.args.get('link') or request.form.get('link', '')
        link = link.strip()
        
        if not link:
            flash('Please provide a valid YouTube URL', 'error')
            return redirect(url_for('video_downloader_home'))
        
        # Create downloads directory
        downloads_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'video_downloads')
        os.makedirs(downloads_dir, exist_ok=True)
        
        # Clean old downloads (keep only last 3 files)
        existing_files = os.listdir(downloads_dir)
        if len(existing_files) > 3:
            for f in existing_files:
                os.remove(os.path.join(downloads_dir, f))
        
        clean = clean_youtube_url(link)
        yt = YouTube(clean)
        
        stream = yt.streams.get_lowest_resolution()
        filename = f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
        out_file = stream.download(output_path=downloads_dir, filename=filename)
        
        return send_from_directory(downloads_dir, filename, as_attachment=True)
    
    except Exception as e:
        logging.error(f"Error downloading video: {e}")
        flash(f'Error downloading video: {str(e)}', 'error')
        return redirect(url_for('video_downloader_home'))

@app.route('/submit_audio', methods=['GET', 'POST'])
@login_required
def submit_audio_download():
    """Download YouTube audio using PyTubeFix"""
    try:
        # Get link from either query parameter (GET) or form data (POST)
        link = request.args.get('link') or request.form.get('link', '')
        link = link.strip()
        
        if not link:
            flash('Please provide a valid YouTube URL', 'error')
            return redirect(url_for('video_downloader_home'))
        
        # Create downloads directory
        downloads_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'audio_downloads')
        os.makedirs(downloads_dir, exist_ok=True)
        
        # Clean old downloads (keep only last 5 files)
        existing_files = os.listdir(downloads_dir)
        if len(existing_files) > 5:
            for f in existing_files:
                os.remove(os.path.join(downloads_dir, f))
        
        clean = clean_youtube_url(link)
        yt = YouTube(clean)
        
        audio_stream = yt.streams.filter(only_audio=True).first()
        filename = f"audio_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3"
        out_file = audio_stream.download(output_path=downloads_dir, filename=filename)
        
        # If ffmpeg is available, convert to proper MP3
        # Otherwise, just rename the file
        try:
            import subprocess
            final_file = out_file.replace('.mp3', '_final.mp3')
            subprocess.run(['ffmpeg', '-y', '-i', out_file, '-c:a', 'libmp3lame', final_file], 
                         check=True, capture_output=True)
            os.remove(out_file)
            out_file = final_file
            filename = os.path.basename(final_file)
        except:
            # ffmpeg not available, use original file
            pass
        
        return send_from_directory(downloads_dir, filename, as_attachment=True)
    
    except Exception as e:
        logging.error(f"Error downloading audio: {e}")
        flash(f'Error downloading audio: {str(e)}', 'error')
        return redirect(url_for('video_downloader_home'))

# QUIZ SYSTEM ROUTES - API ENDPOINT FOR AI MCQ GENERATION
@app.route('/api/generate-mcq-options', methods=['POST'])
@login_required
def generate_mcq_options():
    """Generate MCQ options and correct answer using AI Gemini"""
    try:
        data = request.get_json()
        question = data.get('question', '').strip()
        context = data.get('context', '').strip()
        
        if not question or len(question) < 5:
            return jsonify({'success': False, 'error': 'Question too short'}), 400
        
        # Generate MCQ options using Gemini AI
        result = gemini_ai.generate_mcq_options(question, context)
        
        if result and 'options' in result:
            return jsonify({
                'success': True,
                'options': {
                    'option_a': result['options'].get('A', ''),
                    'option_b': result['options'].get('B', ''),
                    'option_c': result['options'].get('C', ''),
                    'option_d': result['options'].get('D', ''),
                    'correct_answer': result.get('correct_answer', 'A'),
                    'explanation': result.get('explanation', '')
                }
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to generate options'}), 500
            
    except Exception as e:
        logging.error(f"Error generating MCQ options: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/instructor/courses/<int:course_id>/quizzes/generate-ai', methods=['POST'])
@instructor_required
def instructor_generate_ai_quiz(course_id):
    """Generate MCQ quiz questions using AI from a topic and save directly to database"""
    try:
        data = request.get_json()
        title = data.get('title', '').strip()
        topic = data.get('topic', '').strip()
        num_questions = int(data.get('num_questions', 5))
        difficulty = data.get('difficulty', 'medium')
        
        if not title or len(title) < 3:
            return jsonify({'success': False, 'message': 'Please enter a quiz title.'}), 400
        
        if not topic or len(topic) < 3:
            return jsonify({'success': False, 'message': 'Please enter a topic.'}), 400
        
        # Support up to 100 questions per quiz
        if num_questions < 1 or num_questions > 100:
            num_questions = min(max(num_questions, 1), 100)
        
        # Generate questions using Gemini AI
        try:
            questions = gemini_ai.generate_mcq_quiz(topic, num_questions, difficulty)
        except ValueError as ve:
            logging.error(f"Gemini API configuration error: {ve}")
            return jsonify({'success': False, 'message': f'Configuration Error: {str(ve)}'}), 500
        except Exception as e:
            logging.error(f"Error generating quiz: {e}")
            return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500
        
        if not questions or len(questions) == 0:
            return jsonify({'success': False, 'message': 'No questions were generated. Please try a different topic or try again.'}), 500
        
        # Create quiz directly in database
        with db_lock:
            conn = get_db_connection()
            
            # Verify course belongs to instructor
            course = conn.execute('SELECT * FROM courses WHERE id = ? AND instructor_id = ?', 
                                (course_id, current_user.id)).fetchone()
            
            if not course:
                conn.close()
                return jsonify({'success': False, 'message': 'Course not found or access denied.'}), 403
            
            try:
                # Create the quiz/assignment
                total_points = len(questions)  # 1 point per question by default
                cursor = conn.execute('''
                    INSERT INTO assignments (course_id, title, description, instructions, max_points)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    course_id, 
                    title,
                    f"AI-generated quiz on {topic} ({difficulty} difficulty)",
                    "Answer all multiple choice questions. Each question is worth 1 point.",
                    total_points
                ))
                
                assignment_id = cursor.lastrowid
                
                # Save all generated questions
                for q in questions:
                    # Insert question
                    cursor = conn.execute('''
                        INSERT INTO quiz_questions (assignment_id, question_text, question_type, points, correct_answer, explanation)
                        VALUES (?, ?, 'mcq', ?, ?, ?)
                    ''', (assignment_id, q['question'], 1, q['correct_answer'], q.get('explanation', '')))
                    
                    question_id = cursor.lastrowid
                    
                    # Insert options
                    for option_letter, option_text in q['options'].items():
                        is_correct = (option_letter == q['correct_answer'])
                        conn.execute('''
                            INSERT INTO question_options (question_id, option_letter, option_text, is_correct)
                            VALUES (?, ?, ?, ?)
                        ''', (question_id, option_letter, option_text, is_correct))
                
                conn.commit()
                
                logging.info(f"AI Quiz '{topic}' created with {len(questions)} questions (ID: {assignment_id})")
                
                conn.close()
                
                return jsonify({
                    'success': True,
                    'message': f'Quiz created with {len(questions)} questions! You can now edit and submit to students.',
                    'quiz_id': assignment_id,
                    'redirect_url': url_for('instructor_edit_quiz', course_id=course_id, quiz_id=assignment_id)
                })
                
            except Exception as e:
                conn.rollback()
                conn.close()
                logging.error(f"Error saving AI quiz: {e}")
                return jsonify({'success': False, 'message': f'Error saving quiz: {str(e)}'}), 500
        
    except ValueError as ve:
        logging.error(f"Gemini API Key Error: {ve}")
        return jsonify({'success': False, 'message': 'Gemini API not configured. Please set GEMINI_API_KEY.'}), 500
    except Exception as e:
        logging.error(f"Error generating AI quiz: {e}")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/instructor/courses/<int:course_id>/quizzes')
@instructor_required
def instructor_course_quizzes(course_id):
    """Manage course quizzes"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        # Get quizzes (using assignments table)
        quizzes = conn.execute('''
            SELECT a.*, COUNT(s.id) as submission_count
            FROM assignments a
            LEFT JOIN assignment_submissions s ON a.id = s.assignment_id
            WHERE a.course_id = ?
            GROUP BY a.id
            ORDER BY a.created_at DESC
        ''', (course_id,)).fetchall()
        
        conn.close()
    
    return render_template('instructor/course_quizzes.html', course=course, quizzes=quizzes)

@app.route('/instructor/courses/<int:course_id>/quizzes/create', methods=['GET', 'POST'])
@instructor_required
def instructor_create_quiz(course_id):
    """Create a new quiz"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        if request.method == 'POST':
            # Validate CSRF token
            csrf_token = request.form.get('csrf_token')
            if not validate_csrf_token(csrf_token):
                flash('Invalid security token. Please try again.', 'error')
                conn.close()
                return redirect(url_for('instructor_create_quiz', course_id=course_id))
            
            title = request.form.get('title', '').strip()
            description = request.form.get('description', '').strip()
            instructions = request.form.get('instructions', '').strip()
            due_date = request.form.get('due_date') or None
            max_points = int(request.form.get('max_points', 100))
            
            try:
                # Create the quiz/assignment
                cursor = conn.execute('''
                    INSERT INTO assignments (course_id, title, description, instructions, due_date, max_points)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (course_id, title, description, instructions, due_date, max_points))
                
                assignment_id = cursor.lastrowid
                total_points = 0
                
                # Process MCQ questions
                questions_data = {}
                for key in request.form.keys():
                    if key.startswith('questions[') and '][text]' in key:
                        # Extract question number
                        question_num = key.split('[')[1].split(']')[0]
                        if question_num not in questions_data:
                            questions_data[question_num] = {}
                        questions_data[question_num]['text'] = request.form.get(key, '').strip()
                    elif key.startswith('questions[') and '][correct]' in key:
                        question_num = key.split('[')[1].split(']')[0]
                        if question_num not in questions_data:
                            questions_data[question_num] = {}
                        questions_data[question_num]['correct'] = request.form.get(key)
                    elif key.startswith('questions[') and '][explanation]' in key:
                        question_num = key.split('[')[1].split(']')[0]
                        if question_num not in questions_data:
                            questions_data[question_num] = {}
                        questions_data[question_num]['explanation'] = request.form.get(key, '').strip()
                    elif key.startswith('questions[') and '][points]' in key:
                        question_num = key.split('[')[1].split(']')[0]
                        if question_num not in questions_data:
                            questions_data[question_num] = {}
                        questions_data[question_num]['points'] = int(request.form.get(key, 1))
                    elif key.startswith('questions[') and '][option_' in key:
                        question_num = key.split('[')[1].split(']')[0]
                        option_letter = key.split('option_')[1].split(']')[0].upper()
                        if question_num not in questions_data:
                            questions_data[question_num] = {}
                        if 'options' not in questions_data[question_num]:
                            questions_data[question_num]['options'] = {}
                        questions_data[question_num]['options'][option_letter] = request.form.get(key, '').strip()
                
                # Save questions and options
                questions_saved = 0
                for question_num, question_data in questions_data.items():
                    if 'text' in question_data and question_data['text']:
                        # Use instructor-selected correct answer
                        correct_answer = question_data.get('correct', 'A')
                        
                        # Insert question with instructor-selected or AI-generated correct answer
                        cursor = conn.execute('''
                            INSERT INTO quiz_questions (assignment_id, question_text, question_type, points, correct_answer, explanation)
                            VALUES (?, ?, 'mcq', ?, ?, ?)
                        ''', (assignment_id, question_data['text'], question_data.get('points', 1), 
                             correct_answer, question_data.get('explanation', '')))
                        
                        question_id = cursor.lastrowid
                        total_points += question_data.get('points', 1)
                        questions_saved += 1
                        
                        # Insert options with instructor-selected correct answer
                        for option_letter, option_text in question_data.get('options', {}).items():
                            if option_text:
                                is_correct = (option_letter == correct_answer)
                                conn.execute('''
                                    INSERT INTO question_options (question_id, option_letter, option_text, is_correct)
                                    VALUES (?, ?, ?, ?)
                                ''', (question_id, option_letter, option_text, is_correct))
                
                # Update assignment with calculated total points
                conn.execute('''
                    UPDATE assignments SET max_points = ? WHERE id = ?
                ''', (total_points, assignment_id))
                
                conn.commit()
                
                if questions_saved > 0:
                    flash(f'✅ MCQ Quiz "{title}" created successfully with {questions_saved} questions and submitted to all enrolled students!', 'success')
                    logging.info(f"Quiz '{title}' created with {questions_saved} questions for course {course_id}")
                else:
                    flash('⚠️ Quiz created but no questions were saved. Please add questions and try again.', 'warning')
                
                return redirect(url_for('instructor_course_quizzes', course_id=course_id))
                
            except Exception as e:
                conn.rollback()
                flash('Error creating quiz. Please try again.', 'error')
        
        conn.close()
    
    return render_template('instructor/create_quiz.html', course=course)

@app.route('/instructor/courses/<int:course_id>/quizzes/<int:quiz_id>/results')
@instructor_required
def instructor_quiz_results(course_id, quiz_id):
    """View quiz results and submissions"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course ownership and quiz
        quiz = conn.execute('''
            SELECT a.*, c.title as course_title
            FROM assignments a
            JOIN courses c ON a.course_id = c.id
            WHERE a.id = ? AND a.course_id = ? AND c.instructor_id = ?
        ''', (quiz_id, course_id, current_user.id)).fetchone()
        
        if not quiz:
            flash('Quiz not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_course_quizzes', course_id=course_id))
        
        # Get all submissions for this quiz
        submissions = conn.execute('''
            SELECT s.*, u.full_name as student_name, u.username
            FROM assignment_submissions s
            JOIN users u ON s.student_id = u.id
            WHERE s.assignment_id = ?
            ORDER BY s.submitted_at DESC
        ''', (quiz_id,)).fetchall()
        
        # Get basic stats
        stats = {
            'total_submissions': len(submissions),
            'graded_count': len([s for s in submissions if s['grade'] is not None]),
            'average_grade': sum(s['grade'] for s in submissions if s['grade'] is not None) / max(1, len([s for s in submissions if s['grade'] is not None])) if submissions else 0
        }
        
        conn.close()
    
    return render_template('instructor/quiz_results.html', quiz=quiz, submissions=submissions, stats=stats)

@app.route('/instructor/courses/<int:course_id>/quizzes/<int:quiz_id>/edit', methods=['GET', 'POST'])
@instructor_required
def instructor_edit_quiz(course_id, quiz_id):
    """Edit existing quiz"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course ownership and quiz
        quiz = conn.execute('''
            SELECT a.*, c.title as course_title
            FROM assignments a
            JOIN courses c ON a.course_id = c.id
            WHERE a.id = ? AND a.course_id = ? AND c.instructor_id = ?
        ''', (quiz_id, course_id, current_user.id)).fetchone()
        
        if not quiz:
            flash('Quiz not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_course_quizzes', course_id=course_id))
        
        if request.method == 'POST':
            title = request.form.get('title', '').strip()
            description = request.form.get('description', '').strip()
            instructions = request.form.get('instructions', '').strip()
            due_date = request.form.get('due_date') or None
            max_points = int(request.form.get('max_points', 100))
            
            try:
                conn.execute('''
                    UPDATE assignments 
                    SET title = ?, description = ?, instructions = ?, due_date = ?, max_points = ?
                    WHERE id = ?
                ''', (title, description, instructions, due_date, max_points, quiz_id))
                
                # Update MCQ questions if provided
                questions = conn.execute('SELECT id FROM quiz_questions WHERE assignment_id = ?', (quiz_id,)).fetchall()
                for idx, q in enumerate(questions):
                    q_id = q['id']
                    q_text = request.form.get(f'question_{idx}_text', '').strip()
                    explanation = request.form.get(f'question_{idx}_explanation', '').strip()
                    correct = request.form.get(f'question_{idx}_correct', 'A')
                    
                    if q_text:
                        conn.execute('''
                            UPDATE quiz_questions 
                            SET question_text = ?, explanation = ?, correct_answer = ?
                            WHERE id = ?
                        ''', (q_text, explanation, correct, q_id))
                        
                        # Update options
                        for letter in ['A', 'B', 'C', 'D']:
                            option_text = request.form.get(f'question_{idx}_option_{letter}', '').strip()
                            if option_text:
                                conn.execute('''
                                    UPDATE question_options 
                                    SET option_text = ?, is_correct = ?
                                    WHERE question_id = ? AND option_letter = ?
                                ''', (option_text, (letter == correct), q_id, letter))
                
                conn.commit()
                flash(f'Quiz "{title}" updated successfully!', 'success')
                return redirect(url_for('instructor_course_quizzes', course_id=course_id))
                
            except Exception as e:
                conn.rollback()
                flash('Error updating quiz. Please try again.', 'error')
        
        # Fetch questions for editing
        questions = conn.execute('''
            SELECT q.id, q.question_text, q.correct_answer, q.explanation,
                   q.points FROM quiz_questions q
            WHERE q.assignment_id = ?
            ORDER BY q.id
        ''', (quiz_id,)).fetchall()
        
        # Fetch options for each question
        questions_list = []
        for q in questions:
            options = conn.execute('''
                SELECT option_letter, option_text FROM question_options
                WHERE question_id = ? ORDER BY option_letter
            ''', (q['id'],)).fetchall()
            questions_list.append({
                'id': q['id'],
                'text': q['question_text'],
                'correct': q['correct_answer'],
                'explanation': q['explanation'],
                'points': q['points'],
                'options': {opt['option_letter']: opt['option_text'] for opt in options}
            })
        
        conn.close()
    
    return render_template('instructor/edit_quiz.html', quiz=quiz, questions=questions_list)

@app.route('/instructor/courses/<int:course_id>/quizzes/<int:quiz_id>/delete', methods=['POST'])
@instructor_required
def instructor_delete_quiz(course_id, quiz_id):
    """Delete a quiz and all associated data"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course ownership and quiz
        quiz = conn.execute('''
            SELECT a.*, c.title as course_title
            FROM assignments a
            JOIN courses c ON a.course_id = c.id
            WHERE a.id = ? AND a.course_id = ? AND c.instructor_id = ?
        ''', (quiz_id, course_id, current_user.id)).fetchone()
        
        if not quiz:
            flash('Quiz not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_course_quizzes', course_id=course_id))
        
        try:
            # Delete quiz questions first
            conn.execute('DELETE FROM quiz_questions WHERE assignment_id = ?', (quiz_id,))
            
            # Delete student MCQ answers
            conn.execute('''
                DELETE FROM student_mcq_answers 
                WHERE submission_id IN (
                    SELECT id FROM assignment_submissions WHERE assignment_id = ?
                )
            ''', (quiz_id,))
            
            # Delete submissions
            conn.execute('DELETE FROM assignment_submissions WHERE assignment_id = ?', (quiz_id,))
            
            # Delete the quiz itself
            conn.execute('DELETE FROM assignments WHERE id = ?', (quiz_id,))
            
            conn.commit()
            flash(f'Quiz "{quiz["title"]}" and all associated data deleted successfully.', 'success')
            
        except Exception as e:
            conn.rollback()
            flash('Error deleting quiz. Please try again.', 'error')
            print(f"Delete quiz error: {e}")
        
        conn.close()
    
    return redirect(url_for('instructor_course_quizzes', course_id=course_id))

@app.route('/instructor/courses/<int:course_id>/quizzes/<int:quiz_id>/analytics')
@instructor_required
def instructor_quiz_analytics(course_id, quiz_id):
    """Basic quiz analytics"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course ownership and quiz
        quiz = conn.execute('''
            SELECT a.*, c.title as course_title
            FROM assignments a
            JOIN courses c ON a.course_id = c.id
            WHERE a.id = ? AND a.course_id = ? AND c.instructor_id = ?
        ''', (quiz_id, course_id, current_user.id)).fetchone()
        
        if not quiz:
            flash('Quiz not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_course_quizzes', course_id=course_id))
        
        # Get enrollment count for completion rate
        enrolled_students = conn.execute('''
            SELECT COUNT(*) as count FROM enrollments 
            WHERE course_id = ? AND status = 'approved'
        ''', (course_id,)).fetchone()['count']
        
        # Get submission analytics
        submissions = conn.execute('''
            SELECT grade FROM assignment_submissions 
            WHERE assignment_id = ? AND grade IS NOT NULL
        ''', (quiz_id,)).fetchall()
        
        grades = [s['grade'] for s in submissions]
        analytics = {
            'enrolled_students': enrolled_students,
            'submission_count': len(submissions),
            'completion_rate': (len(submissions) / max(1, enrolled_students)) * 100,
            'average_grade': sum(grades) / max(1, len(grades)) if grades else 0,
            'highest_grade': max(grades) if grades else 0,
            'lowest_grade': min(grades) if grades else 0,
            'grade_distribution': {
                'A (90-100)': len([g for g in grades if g >= 90]),
                'B (80-89)': len([g for g in grades if 80 <= g < 90]),
                'C (70-79)': len([g for g in grades if 70 <= g < 80]),
                'D (60-69)': len([g for g in grades if 60 <= g < 70]),
                'F (0-59)': len([g for g in grades if g < 60])
            }
        }
        
        conn.close()
    
    return render_template('instructor/quiz_analytics.html', quiz=quiz, analytics=analytics)

# ASSIGNMENT MANAGEMENT ROUTES
@app.route('/instructor/courses/<int:course_id>/assignments')
@instructor_required
def instructor_course_assignments(course_id):
    """View all assignments for a course with admin panel"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        # Get all assignments with submission counts
        assignments = conn.execute('''
            SELECT a.*, 
                   COUNT(DISTINCT s.id) as total_submissions,
                   COUNT(DISTINCT CASE WHEN s.grade IS NOT NULL THEN s.id END) as graded_count,
                   COUNT(DISTINCT e.id) as enrolled_students
            FROM assignments a
            LEFT JOIN assignment_submissions s ON a.id = s.assignment_id
            LEFT JOIN enrollments e ON e.course_id = a.course_id AND e.status = 'approved'
            WHERE a.course_id = ?
            GROUP BY a.id
            ORDER BY a.created_at DESC
        ''', (course_id,)).fetchall()
        
        conn.close()
    
    return render_template('instructor/course_assignments.html', course=course, assignments=assignments)

@app.route('/instructor/courses/<int:course_id>/assignments/create', methods=['GET', 'POST'])
@instructor_required
def instructor_create_assignment(course_id):
    """Create a new assignment with full VIP features"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        if request.method == 'POST':
            # Validate CSRF token
            csrf_token = request.form.get('csrf_token')
            if not validate_csrf_token(csrf_token):
                flash('Invalid security token. Please try again.', 'error')
                conn.close()
                return redirect(url_for('instructor_create_assignment', course_id=course_id))
            
            title = request.form.get('title', '').strip()
            description = request.form.get('description', '').strip()
            instructions = request.form.get('instructions', '').strip()
            assignment_type = request.form.get('assignment_type', 'essay')
            due_date = request.form.get('due_date') or None
            max_points = int(request.form.get('max_points', 100))
            allow_late = request.form.get('allow_late_submission') == 'on'
            status = request.form.get('status', 'draft')
            
            if not title:
                flash('Assignment title is required.', 'error')
                conn.close()
                return redirect(url_for('instructor_create_assignment', course_id=course_id))
            
            try:
                # Create the assignment
                cursor = conn.execute('''
                    INSERT INTO assignments (
                        course_id, title, description, instructions, 
                        assignment_type, due_date, max_points, 
                        allow_late_submission, status, published_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    course_id, title, description, instructions,
                    assignment_type, due_date, max_points,
                    allow_late, status,
                    datetime.now() if status == 'published' else None
                ))
                
                assignment_id = cursor.lastrowid
                
                # Handle file uploads (assignment materials)
                if 'assignment_files' in request.files:
                    files = request.files.getlist('assignment_files')
                    for file in files:
                        if file and file.filename:
                            filename = secure_filename(file.filename)
                            file_path = os.path.join(
                                app.config['UPLOAD_FOLDER'], 
                                'assignments',
                                f"{assignment_id}_{uuid.uuid4().hex}_{filename}"
                            )
                            
                            # Create directory if it doesn't exist
                            os.makedirs(os.path.dirname(file_path), exist_ok=True)
                            file.save(file_path)
                            
                            # Get file info
                            file_size = os.path.getsize(file_path)
                            file_type = filename.rsplit('.', 1)[1].lower() if '.' in filename else 'unknown'
                            
                            # Save to database
                            conn.execute('''
                                INSERT INTO assignment_assets (
                                    assignment_id, file_name, file_path, file_type, file_size
                                )
                                VALUES (?, ?, ?, ?, ?)
                            ''', (assignment_id, filename, file_path, file_type, file_size))
                
                conn.commit()
                flash(f'Assignment "{title}" created successfully!', 'success')
                conn.close()
                return redirect(url_for('instructor_course_assignments', course_id=course_id))
                
            except Exception as e:
                conn.rollback()
                flash('Error creating assignment. Please try again.', 'error')
                print(f"Create assignment error: {e}")
                conn.close()
                return redirect(url_for('instructor_create_assignment', course_id=course_id))
        
        conn.close()
    
    return render_template('instructor/create_assignment.html', course=course)

@app.route('/instructor/courses/<int:course_id>/assignments/<int:assignment_id>/submissions')
@instructor_required
def instructor_assignment_submissions(course_id, assignment_id):
    """View all submissions for an assignment - Admin Panel"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify ownership
        assignment = conn.execute('''
            SELECT a.*, c.title as course_title
            FROM assignments a
            JOIN courses c ON a.course_id = c.id
            WHERE a.id = ? AND a.course_id = ? AND c.instructor_id = ?
        ''', (assignment_id, course_id, current_user.id)).fetchone()
        
        if not assignment:
            flash('Assignment not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_course_assignments', course_id=course_id))
        
        # Get all enrolled students with their submission status
        students_data = conn.execute('''
            SELECT 
                u.id as student_id,
                u.full_name,
                u.email,
                e.enrolled_at,
                s.id as submission_id,
                s.submission_text,
                s.file_path,
                s.submitted_at,
                s.grade,
                s.ai_feedback,
                s.instructor_feedback,
                s.graded_at,
                CASE 
                    WHEN s.id IS NULL THEN 'not_submitted'
                    WHEN s.grade IS NULL THEN 'submitted'
                    ELSE 'graded'
                END as status
            FROM enrollments e
            JOIN users u ON e.student_id = u.id
            LEFT JOIN assignment_submissions s ON s.assignment_id = ? AND s.student_id = u.id
            WHERE e.course_id = ? AND e.status = 'approved'
            ORDER BY 
                CASE 
                    WHEN s.id IS NULL THEN 2
                    WHEN s.grade IS NULL THEN 1
                    ELSE 3
                END,
                s.submitted_at DESC,
                u.full_name ASC
        ''', (assignment_id, course_id)).fetchall()
        
        # Get assignment assets
        assets = conn.execute('''
            SELECT * FROM assignment_assets 
            WHERE assignment_id = ?
            ORDER BY uploaded_at DESC
        ''', (assignment_id,)).fetchall()
        
        # Calculate statistics
        total_students = len(students_data)
        submitted = len([s for s in students_data if s['submission_id'] is not None])
        graded = len([s for s in students_data if s['grade'] is not None])
        pending_grading = submitted - graded
        not_submitted = total_students - submitted
        
        stats = {
            'total_students': total_students,
            'submitted': submitted,
            'graded': graded,
            'pending_grading': pending_grading,
            'not_submitted': not_submitted,
            'submission_rate': (submitted / max(1, total_students)) * 100
        }
        
        conn.close()
    
    return render_template('instructor/assignment_submissions.html', 
                         assignment=assignment, 
                         students=students_data,
                         stats=stats,
                         assets=assets)

@app.route('/instructor/courses/<int:course_id>/assignments/<int:assignment_id>/grade/<int:submission_id>', methods=['POST'])
@instructor_required
def instructor_grade_submission(course_id, assignment_id, submission_id):
    """Grade a student's assignment submission"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify ownership
        assignment = conn.execute('''
            SELECT a.*
            FROM assignments a
            JOIN courses c ON a.course_id = c.id
            WHERE a.id = ? AND a.course_id = ? AND c.instructor_id = ?
        ''', (assignment_id, course_id, current_user.id)).fetchone()
        
        if not assignment:
            conn.close()
            return jsonify({'success': False, 'message': 'Access denied'}), 403
        
        grade = request.form.get('grade', '').strip()
        feedback = request.form.get('instructor_feedback', '').strip()
        
        try:
            grade_value = float(grade) if grade else None
            
            # Validate grade range
            if grade_value is not None and (grade_value < 0 or grade_value > assignment['max_points']):
                conn.close()
                return jsonify({
                    'success': False, 
                    'message': f'Grade must be between 0 and {assignment["max_points"]}'
                }), 400
            
            # Update submission
            conn.execute('''
                UPDATE assignment_submissions 
                SET grade = ?, 
                    instructor_feedback = ?,
                    graded_at = ?
                WHERE id = ?
            ''', (grade_value, feedback, datetime.now(), submission_id))
            
            conn.commit()
            conn.close()
            
            return jsonify({'success': True, 'message': 'Submission graded successfully'})
            
        except ValueError:
            conn.close()
            return jsonify({'success': False, 'message': 'Invalid grade value'}), 400
        except Exception as e:
            conn.rollback()
            conn.close()
            return jsonify({'success': False, 'message': str(e)}), 500

# DISCUSSION FORUMS ROUTES
@app.route('/instructor/courses/<int:course_id>/discussions')
@instructor_required
def instructor_course_discussions(course_id):
    """Manage course discussion forums"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        # Get course forums and recent topics
        forums = conn.execute('''
            SELECT f.*, COUNT(t.id) as topic_count
            FROM forums f
            LEFT JOIN forum_topics t ON f.id = t.forum_id
            WHERE f.course_id = ? AND f.is_active = 1
            GROUP BY f.id
            ORDER BY f.created_at DESC
        ''', (course_id,)).fetchall()
        
        # Get recent forum activity
        recent_topics = conn.execute('''
            SELECT t.*, u.full_name as author_name, f.title as forum_title,
                   COUNT(r.id) as reply_count
            FROM forum_topics t
            JOIN users u ON t.user_id = u.id
            JOIN forums f ON t.forum_id = f.id
            LEFT JOIN forum_replies r ON t.id = r.topic_id
            WHERE f.course_id = ?
            GROUP BY t.id
            ORDER BY t.created_at DESC
            LIMIT 10
        ''', (course_id,)).fetchall()
        
        conn.close()
    
    return render_template('instructor/course_discussions.html', 
                         course=course, forums=forums, recent_topics=recent_topics)

@app.route('/instructor/courses/<int:course_id>/forums/create', methods=['POST'])
@instructor_required
def instructor_create_forum(course_id):
    """Create a new discussion forum"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        
        if not title:
            flash('Forum title is required.', 'error')
            conn.close()
            return redirect(url_for('instructor_course_discussions', course_id=course_id))
        
        try:
            conn.execute('''
                INSERT INTO forums (course_id, title, description)
                VALUES (?, ?, ?)
            ''', (course_id, title, description))
            
            conn.commit()
            flash(f'Discussion forum "{title}" created successfully!', 'success')
            
        except Exception as e:
            conn.rollback()
            flash('Error creating forum. Please try again.', 'error')
        
        conn.close()
    
    return redirect(url_for('instructor_course_discussions', course_id=course_id))

@app.route('/instructor/courses/<int:course_id>/topics/create', methods=['POST'])
@instructor_required
def instructor_create_topic(course_id):
    """Create a new discussion topic"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        forum_id = request.form.get('forum_id')
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        is_pinned = bool(request.form.get('is_pinned'))
        
        if not title or not content or not forum_id:
            flash('Topic title, content, and forum selection are required.', 'error')
            conn.close()
            return redirect(url_for('instructor_course_discussions', course_id=course_id))
        
        # Verify forum belongs to this course
        forum = conn.execute('''
            SELECT * FROM forums 
            WHERE id = ? AND course_id = ? AND is_active = 1
        ''', (forum_id, course_id)).fetchone()
        
        if not forum:
            flash('Forum not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_course_discussions', course_id=course_id))
        
        try:
            conn.execute('''
                INSERT INTO forum_topics (forum_id, user_id, title, content, is_pinned)
                VALUES (?, ?, ?, ?, ?)
            ''', (forum_id, current_user.id, title, content, is_pinned))
            
            conn.commit()
            flash(f'Discussion topic "{title}" created successfully!', 'success')
            
        except Exception as e:
            conn.rollback()
            flash('Error creating topic. Please try again.', 'error')
        
        conn.close()
    
    return redirect(url_for('instructor_course_discussions', course_id=course_id))


@app.route('/instructor/courses/<int:course_id>/forums/<int:forum_id>/topics')
@instructor_required
def instructor_forum_topics(course_id, forum_id):
    """View all topics in a forum"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor or admin
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND (instructor_id = ? OR ? = 1) AND is_active = 1
        ''', (course_id, current_user.id, current_user.is_admin())).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        # Verify forum belongs to this course
        forum = conn.execute('''
            SELECT * FROM forums 
            WHERE id = ? AND course_id = ? AND is_active = 1
        ''', (forum_id, course_id)).fetchone()
        
        if not forum:
            flash('Forum not found.', 'error')
            conn.close()
            return redirect(url_for('instructor_course_discussions', course_id=course_id))
        
        # Get all topics in this forum
        topics = conn.execute('''
            SELECT t.*, u.full_name as author_name, COUNT(r.id) as reply_count,
                   MAX(COALESCE(r.created_at, t.created_at)) as last_activity
            FROM forum_topics t
            JOIN users u ON t.user_id = u.id
            LEFT JOIN forum_replies r ON t.id = r.topic_id
            WHERE t.forum_id = ?
            GROUP BY t.id
            ORDER BY t.is_pinned DESC, last_activity DESC
        ''', (forum_id,)).fetchall()
        
        conn.close()
    
    return render_template('instructor/forum_topics.html', 
                         course=course, forum=forum, topics=topics)


@app.route('/instructor/courses/<int:course_id>/forums/<int:forum_id>/topics/<int:topic_id>')
@instructor_required
def instructor_topic_detail(course_id, forum_id, topic_id):
    """View topic details with replies"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor or admin
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND (instructor_id = ? OR ? = 1) AND is_active = 1
        ''', (course_id, current_user.id, current_user.is_admin())).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        # Verify forum belongs to this course
        forum = conn.execute('''
            SELECT * FROM forums 
            WHERE id = ? AND course_id = ? AND is_active = 1
        ''', (forum_id, course_id)).fetchone()
        
        if not forum:
            flash('Forum not found.', 'error')
            conn.close()
            return redirect(url_for('instructor_course_discussions', course_id=course_id))
        
        # Get topic details
        topic = conn.execute('''
            SELECT t.*, u.full_name as author_name, u.role as author_role
            FROM forum_topics t
            JOIN users u ON t.user_id = u.id
            WHERE t.id = ? AND t.forum_id = ?
        ''', (topic_id, forum_id)).fetchone()
        
        if not topic:
            flash('Topic not found.', 'error')
            conn.close()
            return redirect(url_for('instructor_forum_topics', course_id=course_id, forum_id=forum_id))
        
        # Get replies
        replies = conn.execute('''
            SELECT r.*, u.full_name as author_name, u.role as author_role
            FROM forum_replies r
            JOIN users u ON r.user_id = u.id
            WHERE r.topic_id = ?
            ORDER BY r.created_at ASC
        ''', (topic_id,)).fetchall()
        
        # Update view count
        conn.execute('UPDATE forum_topics SET view_count = view_count + 1 WHERE id = ?', (topic_id,))
        conn.commit()
        
        conn.close()
    
    return render_template('instructor/topic_detail.html', 
                         course=course, forum=forum, topic=topic, replies=replies)


@app.route('/instructor/courses/<int:course_id>/forums/<int:forum_id>/edit')
@instructor_required
def instructor_edit_forum(course_id, forum_id):
    """Edit forum form"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        # Get forum details
        forum = conn.execute('''
            SELECT * FROM forums 
            WHERE id = ? AND course_id = ? AND is_active = 1
        ''', (forum_id, course_id)).fetchone()
        
        if not forum:
            flash('Forum not found.', 'error')
            conn.close()
            return redirect(url_for('instructor_course_discussions', course_id=course_id))
        
        conn.close()
    
    return render_template('instructor/edit_forum.html', course=course, forum=forum)


@app.route('/instructor/courses/<int:course_id>/forums/<int:forum_id>/update', methods=['POST'])
@instructor_required
def instructor_update_forum(course_id, forum_id):
    """Update forum details"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        
        if not title:
            flash('Forum title is required.', 'error')
            conn.close()
            return redirect(url_for('instructor_edit_forum', course_id=course_id, forum_id=forum_id))
        
        try:
            conn.execute('''
                UPDATE forums 
                SET title = ?, description = ?
                WHERE id = ? AND course_id = ?
            ''', (title, description, forum_id, course_id))
            
            conn.commit()
            flash(f'Forum "{title}" updated successfully!', 'success')
            
        except Exception as e:
            conn.rollback()
            flash('Error updating forum. Please try again.', 'error')
        
        conn.close()
    
    return redirect(url_for('instructor_course_discussions', course_id=course_id))


@app.route('/instructor/courses/<int:course_id>/forums/<int:forum_id>/delete', methods=['POST'])
@instructor_required
def instructor_delete_forum(course_id, forum_id):
    """Delete forum (soft delete)"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND instructor_id = ? AND is_active = 1
        ''', (course_id, current_user.id)).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        try:
            # Soft delete forum
            conn.execute('''
                UPDATE forums 
                SET is_active = 0
                WHERE id = ? AND course_id = ?
            ''', (forum_id, course_id))
            
            conn.commit()
            flash('Forum deleted successfully!', 'success')
            
        except Exception as e:
            conn.rollback()
            flash('Error deleting forum. Please try again.', 'error')
        
        conn.close()
    
    return redirect(url_for('instructor_course_discussions', course_id=course_id))


@app.route('/instructor/courses/<int:course_id>/forums/<int:forum_id>/topics/<int:topic_id>/reply', methods=['POST'])
@instructor_required
def instructor_create_reply(course_id, forum_id, topic_id):
    """Create a reply to a discussion topic"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify course belongs to instructor or admin
        course = conn.execute('''
            SELECT * FROM courses 
            WHERE id = ? AND (instructor_id = ? OR ? = 1) AND is_active = 1
        ''', (course_id, current_user.id, current_user.is_admin())).fetchone()
        
        if not course:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('instructor_courses'))
        
        # Verify forum and topic exist
        topic = conn.execute('''
            SELECT t.*, f.course_id
            FROM forum_topics t
            JOIN forums f ON t.forum_id = f.id
            WHERE t.id = ? AND t.forum_id = ? AND f.course_id = ? AND f.is_active = 1
        ''', (topic_id, forum_id, course_id)).fetchone()
        
        if not topic:
            flash('Topic not found.', 'error')
            conn.close()
            return redirect(url_for('instructor_course_discussions', course_id=course_id))
        
        content = request.form.get('content', '').strip()
        
        if not content:
            flash('Reply content is required.', 'error')
            conn.close()
            return redirect(url_for('instructor_topic_detail', course_id=course_id, forum_id=forum_id, topic_id=topic_id))
        
        try:
            conn.execute('''
                INSERT INTO forum_replies (topic_id, user_id, content)
                VALUES (?, ?, ?)
            ''', (topic_id, current_user.id, content))
            
            conn.commit()
            flash('Reply posted successfully!', 'success')
            
        except Exception as e:
            conn.rollback()
            flash('Error posting reply. Please try again.', 'error')
        
        conn.close()
    
    return redirect(url_for('instructor_topic_detail', course_id=course_id, forum_id=forum_id, topic_id=topic_id))


# STUDENT CONTENT ACCESS ROUTES
@app.route('/student/courses/<int:course_id>')
@login_required
def student_course_view(course_id):
    """View course content as student"""
    if not current_user.is_student():
        flash('Access denied. Student account required.', 'error')
        return redirect(url_for('dashboard'))
    
    with db_lock:
        conn = get_db_connection()
        
        # Verify student is enrolled and approved
        enrollment = conn.execute('''
            SELECT e.*, c.*, u.full_name as instructor_name,
                   COALESCE(e.manual_progress_override, e.progress_percentage) as display_progress
            FROM enrollments e
            JOIN courses c ON e.course_id = c.id
            JOIN users u ON c.instructor_id = u.id
            WHERE e.student_id = ? AND e.course_id = ? AND e.status = 'approved'
        ''', (current_user.id, course_id)).fetchone()
        
        if not enrollment:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('dashboard'))
        
        # Get course content
        resources = conn.execute('''
            SELECT * FROM course_resources 
            WHERE course_id = ?
            ORDER BY upload_date DESC
        ''', (course_id,)).fetchall()
        
        # Get meeting links
        meeting_links = conn.execute('''
            SELECT * FROM course_meeting_links 
            WHERE course_id = ? AND is_active = 1
            ORDER BY created_at DESC
        ''', (course_id,)).fetchall()
        
        # Get video playlist
        video_playlist = conn.execute('''
            SELECT * FROM course_video_playlists 
            WHERE course_id = ? AND is_active = 1
            ORDER BY order_index ASC
        ''', (course_id,)).fetchall()
        
        # Get quizzes (assignment_type = 'quiz')
        quizzes = conn.execute('''
            SELECT a.*, 
                   s.id as submission_id, 
                   s.grade, 
                   s.submitted_at,
                   CASE WHEN s.id IS NOT NULL THEN 1 ELSE 0 END as is_submitted
            FROM assignments a
            LEFT JOIN assignment_submissions s ON a.id = s.assignment_id AND s.student_id = ?
            WHERE a.course_id = ? AND a.assignment_type = 'quiz' AND a.status = 'published'
            ORDER BY a.created_at DESC
        ''', (current_user.id, course_id)).fetchall()
        
        # Get assignments (non-quiz types)
        assignments = conn.execute('''
            SELECT a.*, 
                   s.id as submission_id, 
                   s.grade, 
                   s.submitted_at,
                   s.submission_text,
                   s.file_path,
                   CASE WHEN s.id IS NOT NULL THEN 1 ELSE 0 END as is_submitted
            FROM assignments a
            LEFT JOIN assignment_submissions s ON a.id = s.assignment_id AND s.student_id = ?
            WHERE a.course_id = ? AND a.assignment_type != 'quiz' AND a.status = 'published'
            ORDER BY a.due_date ASC, a.created_at DESC
        ''', (current_user.id, course_id)).fetchall()
        
        conn.close()
    
    return render_template('student/course_view.html', 
                         enrollment=enrollment, course=enrollment, resources=resources, quizzes=quizzes,
                         assignments=assignments, meeting_links=meeting_links, video_playlist=video_playlist)

# STUDENT ASSIGNMENT ROUTES
@app.route('/student/courses/<int:course_id>/assignments/<int:assignment_id>/submit', methods=['GET', 'POST'])
@login_required
def student_submit_assignment(course_id, assignment_id):
    """Submit an assignment"""
    if not current_user.is_student():
        flash('Access denied. Student account required.', 'error')
        return redirect(url_for('dashboard'))
    
    with db_lock:
        conn = get_db_connection()
        
        # Verify enrollment
        enrollment = conn.execute('''
            SELECT * FROM enrollments 
            WHERE student_id = ? AND course_id = ? AND status = 'approved'
        ''', (current_user.id, course_id)).fetchone()
        
        if not enrollment:
            flash('Access denied.', 'error')
            conn.close()
            return redirect(url_for('dashboard'))
        
        # Get assignment details
        assignment = conn.execute('''
            SELECT a.*, c.title as course_title
            FROM assignments a
            JOIN courses c ON a.course_id = c.id
            WHERE a.id = ? AND a.course_id = ? AND a.status = 'published'
        ''', (assignment_id, course_id)).fetchone()
        
        if not assignment:
            flash('Assignment not found or not available.', 'error')
            conn.close()
            return redirect(url_for('student_course_view', course_id=course_id))
        
        # Check for existing submission
        existing_submission = conn.execute('''
            SELECT * FROM assignment_submissions 
            WHERE assignment_id = ? AND student_id = ?
        ''', (assignment_id, current_user.id)).fetchone()
        
        if request.method == 'POST':
            submission_text = request.form.get('submission_text', '').strip()
            
            if not submission_text and 'submission_file' not in request.files:
                flash('Please provide submission text or upload a file.', 'error')
                conn.close()
                return redirect(url_for('student_submit_assignment', course_id=course_id, assignment_id=assignment_id))
            
            file_path = None
            if 'submission_file' in request.files:
                file = request.files['submission_file']
                if file and file.filename:
                    filename = secure_filename(file.filename)
                    file_path = os.path.join(
                        app.config['UPLOAD_FOLDER'], 
                        'submissions',
                        f"{assignment_id}_{current_user.id}_{uuid.uuid4().hex}_{filename}"
                    )
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                    file.save(file_path)
            
            try:
                if existing_submission:
                    # Update existing submission
                    conn.execute('''
                        UPDATE assignment_submissions 
                        SET submission_text = ?, 
                            file_path = ?, 
                            submitted_at = ?,
                            grade = NULL,
                            ai_feedback = NULL,
                            instructor_feedback = NULL
                        WHERE id = ?
                    ''', (submission_text, file_path, datetime.now(), existing_submission['id']))
                    flash('Assignment resubmitted successfully!', 'success')
                else:
                    # Create new submission
                    conn.execute('''
                        INSERT INTO assignment_submissions (
                            assignment_id, student_id, submission_text, file_path
                        )
                        VALUES (?, ?, ?, ?)
                    ''', (assignment_id, current_user.id, submission_text, file_path))
                    flash('Assignment submitted successfully!', 'success')
                
                conn.commit()
                conn.close()
                return redirect(url_for('student_course_view', course_id=course_id))
                
            except Exception as e:
                conn.rollback()
                flash('Error submitting assignment. Please try again.', 'error')
                print(f"Submit assignment error: {e}")
        
        conn.close()
    
    return render_template('student/submit_assignment.html', 
                         assignment=assignment, 
                         existing_submission=existing_submission,
                         course_id=course_id)

# STUDENT FORUM ACCESS ROUTES
@app.route('/student/courses/<int:course_id>/forums')
@login_required
def student_course_forums(course_id):
    """View course forums as student"""
    if not current_user.is_student():
        flash('Access denied. Student account required.', 'error')
        return redirect(url_for('dashboard'))
    
    with db_lock:
        conn = get_db_connection()
        
        # Verify student is enrolled and approved
        enrollment = conn.execute('''
            SELECT e.*, c.*, u.full_name as instructor_name,
                   COALESCE(e.manual_progress_override, e.progress_percentage) as display_progress
            FROM enrollments e
            JOIN courses c ON e.course_id = c.id
            JOIN users u ON c.instructor_id = u.id
            WHERE e.student_id = ? AND e.course_id = ? AND e.status = 'approved'
        ''', (current_user.id, course_id)).fetchone()
        
        if not enrollment:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('dashboard'))
        
        # Get course forums and recent topics
        forums = conn.execute('''
            SELECT f.*, COUNT(t.id) as topic_count
            FROM forums f
            LEFT JOIN forum_topics t ON f.id = t.forum_id
            WHERE f.course_id = ? AND f.is_active = 1
            GROUP BY f.id
            ORDER BY f.created_at DESC
        ''', (course_id,)).fetchall()
        
        # Get recent forum activity
        recent_topics = conn.execute('''
            SELECT t.*, u.full_name as author_name, f.title as forum_title,
                   COUNT(r.id) as reply_count
            FROM forum_topics t
            JOIN users u ON t.user_id = u.id
            JOIN forums f ON t.forum_id = f.id
            LEFT JOIN forum_replies r ON t.id = r.topic_id
            WHERE f.course_id = ?
            GROUP BY t.id
            ORDER BY t.created_at DESC
            LIMIT 10
        ''', (course_id,)).fetchall()
        
        conn.close()
    
    return render_template('student/course_forums.html', 
                         course=enrollment, forums=forums, recent_topics=recent_topics)

@app.route('/student/courses/<int:course_id>/forums/<int:forum_id>/topics')
@login_required
def student_forum_topics(course_id, forum_id):
    """View all topics in a forum as student"""
    if not current_user.is_student():
        flash('Access denied. Student account required.', 'error')
        return redirect(url_for('dashboard'))
    
    with db_lock:
        conn = get_db_connection()
        
        # Verify student is enrolled and approved
        enrollment = conn.execute('''
            SELECT e.*, c.*, u.full_name as instructor_name,
                   COALESCE(e.manual_progress_override, e.progress_percentage) as display_progress
            FROM enrollments e
            JOIN courses c ON e.course_id = c.id
            JOIN users u ON c.instructor_id = u.id
            WHERE e.student_id = ? AND e.course_id = ? AND e.status = 'approved'
        ''', (current_user.id, course_id)).fetchone()
        
        if not enrollment:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('dashboard'))
        
        # Verify forum belongs to this course
        forum = conn.execute('''
            SELECT * FROM forums 
            WHERE id = ? AND course_id = ? AND is_active = 1
        ''', (forum_id, course_id)).fetchone()
        
        if not forum:
            flash('Forum not found.', 'error')
            conn.close()
            return redirect(url_for('student_course_forums', course_id=course_id))
        
        # Get all topics in this forum
        topics = conn.execute('''
            SELECT t.*, u.full_name as author_name, COUNT(r.id) as reply_count,
                   MAX(COALESCE(r.created_at, t.created_at)) as last_activity
            FROM forum_topics t
            JOIN users u ON t.user_id = u.id
            LEFT JOIN forum_replies r ON t.id = r.topic_id
            WHERE t.forum_id = ?
            GROUP BY t.id
            ORDER BY t.is_pinned DESC, last_activity DESC
        ''', (forum_id,)).fetchall()
        
        conn.close()
    
    return render_template('student/forum_topics.html', 
                         course=enrollment, forum=forum, topics=topics)

@app.route('/student/courses/<int:course_id>/forums/<int:forum_id>/topics/<int:topic_id>')
@login_required
def student_topic_detail(course_id, forum_id, topic_id):
    """View topic details with replies as student"""
    if not current_user.is_student():
        flash('Access denied. Student account required.', 'error')
        return redirect(url_for('dashboard'))
    
    with db_lock:
        conn = get_db_connection()
        
        # Verify student is enrolled and approved
        enrollment = conn.execute('''
            SELECT e.*, c.*, u.full_name as instructor_name,
                   COALESCE(e.manual_progress_override, e.progress_percentage) as display_progress
            FROM enrollments e
            JOIN courses c ON e.course_id = c.id
            JOIN users u ON c.instructor_id = u.id
            WHERE e.student_id = ? AND e.course_id = ? AND e.status = 'approved'
        ''', (current_user.id, course_id)).fetchone()
        
        if not enrollment:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('dashboard'))
        
        # Verify forum belongs to this course
        forum = conn.execute('''
            SELECT * FROM forums 
            WHERE id = ? AND course_id = ? AND is_active = 1
        ''', (forum_id, course_id)).fetchone()
        
        if not forum:
            flash('Forum not found.', 'error')
            conn.close()
            return redirect(url_for('student_course_forums', course_id=course_id))
        
        # Get topic details
        topic = conn.execute('''
            SELECT t.*, u.full_name as author_name, u.role as author_role
            FROM forum_topics t
            JOIN users u ON t.user_id = u.id
            WHERE t.id = ? AND t.forum_id = ?
        ''', (topic_id, forum_id)).fetchone()
        
        if not topic:
            flash('Topic not found.', 'error')
            conn.close()
            return redirect(url_for('student_forum_topics', course_id=course_id, forum_id=forum_id))
        
        # Get replies
        replies = conn.execute('''
            SELECT r.*, u.full_name as author_name, u.role as author_role
            FROM forum_replies r
            JOIN users u ON r.user_id = u.id
            WHERE r.topic_id = ?
            ORDER BY r.created_at ASC
        ''', (topic_id,)).fetchall()
        
        # Update view count
        conn.execute('UPDATE forum_topics SET view_count = view_count + 1 WHERE id = ?', (topic_id,))
        conn.commit()
        
        conn.close()
    
    return render_template('student/topic_detail.html', 
                         course=enrollment, forum=forum, topic=topic, replies=replies)

@app.route('/student/courses/<int:course_id>/forums/<int:forum_id>/topics/<int:topic_id>/reply', methods=['POST'])
@login_required
def student_create_reply(course_id, forum_id, topic_id):
    """Create a reply to a discussion topic as student"""
    if not current_user.is_student():
        flash('Access denied. Student account required.', 'error')
        return redirect(url_for('dashboard'))
    
    with db_lock:
        conn = get_db_connection()
        
        # Verify student is enrolled and approved
        enrollment = conn.execute('''
            SELECT e.status FROM enrollments e
            WHERE e.student_id = ? AND e.course_id = ? AND e.status = 'approved'
        ''', (current_user.id, course_id)).fetchone()
        
        if not enrollment:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('dashboard'))
        
        # Verify forum and topic exist
        topic = conn.execute('''
            SELECT t.*, f.course_id
            FROM forum_topics t
            JOIN forums f ON t.forum_id = f.id
            WHERE t.id = ? AND t.forum_id = ? AND f.course_id = ? AND f.is_active = 1
        ''', (topic_id, forum_id, course_id)).fetchone()
        
        if not topic:
            flash('Topic not found.', 'error')
            conn.close()
            return redirect(url_for('student_course_forums', course_id=course_id))
        
        content = request.form.get('content', '').strip()
        
        if not content:
            flash('Reply content is required.', 'error')
            conn.close()
            return redirect(url_for('student_topic_detail', course_id=course_id, forum_id=forum_id, topic_id=topic_id))
        
        try:
            conn.execute('''
                INSERT INTO forum_replies (topic_id, user_id, content)
                VALUES (?, ?, ?)
            ''', (topic_id, current_user.id, content))
            
            conn.commit()
            flash('Reply posted successfully!', 'success')
            
        except Exception as e:
            conn.rollback()
            flash('Error posting reply. Please try again.', 'error')
        
        conn.close()
    
    return redirect(url_for('student_topic_detail', course_id=course_id, forum_id=forum_id, topic_id=topic_id))

@app.route('/student/courses/<int:course_id>/forums/<int:forum_id>/topics/create', methods=['POST'])
@login_required
def student_create_topic(course_id, forum_id):
    """Create a new discussion topic as student"""
    if not current_user.is_student():
        flash('Access denied. Student account required.', 'error')
        return redirect(url_for('dashboard'))
    
    with db_lock:
        conn = get_db_connection()
        
        # Verify student is enrolled and approved
        enrollment = conn.execute('''
            SELECT e.status FROM enrollments e
            WHERE e.student_id = ? AND e.course_id = ? AND e.status = 'approved'
        ''', (current_user.id, course_id)).fetchone()
        
        if not enrollment:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('dashboard'))
        
        # Verify forum belongs to this course
        forum = conn.execute('''
            SELECT * FROM forums 
            WHERE id = ? AND course_id = ? AND is_active = 1
        ''', (forum_id, course_id)).fetchone()
        
        if not forum:
            flash('Forum not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('student_course_forums', course_id=course_id))
        
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        
        if not title or not content:
            flash('Topic title and content are required.', 'error')
            conn.close()
            return redirect(url_for('student_forum_topics', course_id=course_id, forum_id=forum_id))
        
        try:
            conn.execute('''
                INSERT INTO forum_topics (forum_id, user_id, title, content, is_pinned)
                VALUES (?, ?, ?, ?, 0)
            ''', (forum_id, current_user.id, title, content))
            
            conn.commit()
            flash(f'Discussion topic "{title}" created successfully!', 'success')
            
        except Exception as e:
            conn.rollback()
            flash('Error creating topic. Please try again.', 'error')
        
        conn.close()
    
    return redirect(url_for('student_forum_topics', course_id=course_id, forum_id=forum_id))

@app.route('/uploads/instructor_screenshots/<filename>')
@login_required
@admin_required
def instructor_screenshot(filename):
    """Serve instructor screenshot files (admin only)"""
    screenshots_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'instructor_screenshots')
    return send_from_directory(screenshots_dir, filename)

@app.route('/student/courses/<int:course_id>/resource/<int:resource_id>')
@login_required
def student_download_resource(course_id, resource_id):
    """Secure file download for enrolled students"""
    if not current_user.is_student():
        flash('Access denied. Student account required.', 'error')
        return redirect(url_for('dashboard'))
    
    with db_lock:
        conn = get_db_connection()
        
        # Verify student is enrolled and approved in the course
        enrollment = conn.execute('''
            SELECT e.status FROM enrollments e
            WHERE e.student_id = ? AND e.course_id = ? AND e.status = 'approved'
        ''', (current_user.id, course_id)).fetchone()
        
        if not enrollment:
            flash('Access denied. You are not enrolled in this course.', 'error')
            conn.close()
            return redirect(url_for('dashboard'))
        
        # Get the resource and verify it belongs to this course
        resource = conn.execute('''
            SELECT * FROM course_resources 
            WHERE id = ? AND course_id = ?
        ''', (resource_id, course_id)).fetchone()
        
        conn.close()
        
        if not resource:
            flash('Resource not found.', 'error')
            return redirect(url_for('student_course_view', course_id=course_id))
        
        # Serve the file securely
        try:
            return send_from_directory(
                os.path.dirname(resource['file_path']),
                os.path.basename(resource['file_path']),
                as_attachment=False
            )
        except FileNotFoundError:
            flash('File not found on server.', 'error')
            return redirect(url_for('student_course_view', course_id=course_id))

@app.route('/student/quiz/<int:quiz_id>')
@login_required
def student_take_quiz(quiz_id):
    """Take a quiz with MCQ questions"""
    if not current_user.is_student():
        flash('Access denied. Student account required.', 'error')
        return redirect(url_for('dashboard'))
    
    with db_lock:
        conn = get_db_connection()
        
        # Verify student is enrolled in the course
        quiz = conn.execute('''
            SELECT a.*, c.title as course_title, e.status as enrollment_status
            FROM assignments a
            JOIN courses c ON a.course_id = c.id
            LEFT JOIN enrollments e ON c.id = e.course_id AND e.student_id = ?
            WHERE a.id = ? AND e.status = 'approved'
        ''', (current_user.id, quiz_id)).fetchone()
        
        if not quiz:
            flash('Quiz not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('dashboard'))
        
        # Check if already submitted
        submission = conn.execute('''
            SELECT * FROM assignment_submissions 
            WHERE assignment_id = ? AND student_id = ?
        ''', (quiz_id, current_user.id)).fetchone()
        
        # Get MCQ questions and options
        questions = conn.execute('''
            SELECT q.*, GROUP_CONCAT(
                o.option_letter || '|' || o.option_text || '|' || o.is_correct, ':'
            ) as options_data
            FROM quiz_questions q
            LEFT JOIN question_options o ON q.id = o.question_id
            WHERE q.assignment_id = ?
            GROUP BY q.id
            ORDER BY q.id
        ''', (quiz_id,)).fetchall()
        
        # Process questions and options
        processed_questions = []
        for question in questions:
            question_dict = dict(question)
            question_dict['options'] = {}
            
            if question['options_data']:
                for option_data in question['options_data'].split(':'):
                    if option_data:
                        parts = option_data.split('|')
                        if len(parts) >= 3:
                            letter = parts[0]
                            text = parts[1]
                            is_correct = parts[2] == '1'
                            question_dict['options'][letter] = {
                                'text': text,
                                'is_correct': is_correct
                            }
            
            processed_questions.append(question_dict)
        
        # Get student's previous answers if any
        student_answers = {}
        if submission:
            answers = conn.execute('''
                SELECT question_id, selected_option 
                FROM student_mcq_answers 
                WHERE submission_id = ?
            ''', (submission['id'],)).fetchall()
            
            for answer in answers:
                student_answers[answer['question_id']] = answer['selected_option']
        
        conn.close()
    
    return render_template('student/take_quiz.html', 
                         quiz=quiz, 
                         submission=submission, 
                         questions=processed_questions,
                         student_answers=student_answers)

@app.route('/student/quiz/<int:quiz_id>/submit', methods=['POST'])
@login_required
def student_submit_quiz(quiz_id):
    """Submit MCQ quiz answers"""
    if not current_user.is_student():
        flash('Access denied. Student account required.', 'error')
        return redirect(url_for('dashboard'))
    
    with db_lock:
        conn = get_db_connection()
        
        # Verify access
        quiz = conn.execute('''
            SELECT a.*, c.title as course_title, e.status as enrollment_status
            FROM assignments a
            JOIN courses c ON a.course_id = c.id
            LEFT JOIN enrollments e ON c.id = e.course_id AND e.student_id = ?
            WHERE a.id = ? AND e.status = 'approved'
        ''', (current_user.id, quiz_id)).fetchone()
        
        if not quiz:
            flash('Quiz not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('dashboard'))
        
        # Get quiz questions for grading
        questions = conn.execute('''
            SELECT q.id, q.points, q.correct_answer
            FROM quiz_questions q
            WHERE q.assignment_id = ?
            ORDER BY q.id
        ''', (quiz_id,)).fetchall()
        
        try:
            # Check if already submitted
            existing = conn.execute('''
                SELECT id FROM assignment_submissions 
                WHERE assignment_id = ? AND student_id = ?
            ''', (quiz_id, current_user.id)).fetchone()
            
            # Calculate score from MCQ answers
            total_score = 0
            total_possible = 0
            mcq_answers = []
            
            for question in questions:
                question_key = f'question_{question["id"]}'
                selected_answer = request.form.get(question_key, '').strip()
                
                if selected_answer:
                    is_correct = (selected_answer == question['correct_answer'])
                    points_earned = question['points'] if is_correct else 0
                    total_score += points_earned
                    
                    mcq_answers.append({
                        'question_id': question['id'],
                        'selected_option': selected_answer,
                        'is_correct': is_correct,
                        'points_earned': points_earned
                    })
                
                total_possible += question['points']
            
            # Create submission text summary
            submission_text = f"MCQ Quiz Submission - {len(mcq_answers)} questions answered"
            
            if existing:
                # Update existing submission
                conn.execute('''
                    UPDATE assignment_submissions 
                    SET submission_text = ?, submitted_at = CURRENT_TIMESTAMP, grade = ?
                    WHERE assignment_id = ? AND student_id = ?
                ''', (submission_text, total_score, quiz_id, current_user.id))
                
                submission_id = existing['id']
                
                # Delete old MCQ answers
                conn.execute('DELETE FROM student_mcq_answers WHERE submission_id = ?', (submission_id,))
            else:
                # Create new submission
                cursor = conn.execute('''
                    INSERT INTO assignment_submissions (assignment_id, student_id, submission_text, grade)
                    VALUES (?, ?, ?, ?)
                ''', (quiz_id, current_user.id, submission_text, total_score))
                
                submission_id = cursor.lastrowid
            
            # Save MCQ answers
            for answer in mcq_answers:
                conn.execute('''
                    INSERT INTO student_mcq_answers 
                    (submission_id, question_id, selected_option, is_correct, points_earned)
                    VALUES (?, ?, ?, ?, ?)
                ''', (submission_id, answer['question_id'], answer['selected_option'], 
                     answer['is_correct'], answer['points_earned']))
            
            # Generate AI feedback
            try:
                ai_feedback = f"Quiz completed! You scored {total_score}/{total_possible} ({(total_score / total_possible * 100) if total_possible > 0 else 0:.1f}%). You answered {sum(1 for ans in mcq_answers if ans['is_correct'])} out of {len(questions)} questions correctly."
                
                conn.execute('''
                    UPDATE assignment_submissions 
                    SET ai_feedback = ?, graded_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (ai_feedback, submission_id))
                
            except Exception as ai_error:
                print(f"AI feedback error: {ai_error}")
            
            conn.commit()
            
            # Update student progress for this course
            update_student_progress(conn, current_user.id, quiz['course_id'])
            
            percentage = (total_score / total_possible * 100) if total_possible > 0 else 0
            flash(f'Quiz submitted successfully! Your score: {total_score}/{total_possible} ({percentage:.1f}%)', 'success')
            
        except Exception as e:
            conn.rollback()
            flash('Error submitting quiz. Please try again.', 'error')
            print(f"Quiz submission error: {e}")
        
        conn.close()
    
    return redirect(url_for('student_take_quiz', quiz_id=quiz_id))

# RANKINGS ROUTES
@app.route('/student/courses/<int:course_id>/rankings')
@login_required
def student_course_rankings(course_id):
    """View course rankings based on quiz scores"""
    if not current_user.is_student():
        flash('Access denied. Student account required.', 'error')
        return redirect(url_for('dashboard'))
    
    with db_lock:
        conn = get_db_connection()
        
        # Verify student is enrolled
        enrollment = conn.execute('''
            SELECT * FROM enrollments e
            JOIN courses c ON e.course_id = c.id
            WHERE e.student_id = ? AND e.course_id = ? AND e.status = 'approved'
        ''', (current_user.id, course_id)).fetchone()
        
        if not enrollment:
            flash('Course not found or access denied.', 'error')
            conn.close()
            return redirect(url_for('dashboard'))
        
        # Get rankings based on quiz scores
        rankings = conn.execute('''
            SELECT u.full_name, AVG(s.grade) as avg_score, COUNT(s.id) as quiz_count,
                   RANK() OVER (ORDER BY AVG(s.grade) DESC) as rank
            FROM users u
            JOIN enrollments e ON u.id = e.student_id
            JOIN assignment_submissions s ON u.id = s.student_id
            JOIN assignments a ON s.assignment_id = a.id
            WHERE e.course_id = ? AND e.status = 'approved' AND s.grade IS NOT NULL AND a.course_id = ?
            GROUP BY u.id, u.full_name
            HAVING quiz_count > 0
            ORDER BY avg_score DESC
        ''', (course_id, course_id)).fetchall()
        
        conn.close()
    
    return render_template('student/course_rankings.html', 
                         course=enrollment, rankings=rankings)

# STUDENT ENROLLMENT ROUTES
@app.route('/student/courses')
@login_required
def student_browse_courses():
    """Browse available courses for enrollment"""
    if not current_user.is_student():
        flash('Access denied. Student account required.', 'error')
        return redirect(url_for('dashboard'))
    
    with db_lock:
        conn = get_db_connection()
        
        # Get available courses (not already enrolled in)
        available_courses = conn.execute('''
            SELECT c.*, u.full_name as instructor_name,
                   COUNT(e.id) as enrollment_count
            FROM courses c
            JOIN users u ON c.instructor_id = u.id
            LEFT JOIN enrollments e ON c.id = e.course_id AND e.status = 'approved'
            LEFT JOIN enrollments student_e ON c.id = student_e.course_id AND student_e.student_id = ?
            WHERE c.is_active = 1 AND student_e.id IS NULL
            GROUP BY c.id
            ORDER BY c.created_at DESC
        ''', (current_user.id,)).fetchall()
        
        # Get my enrollment requests
        my_enrollments = conn.execute('''
            SELECT e.*, c.title as course_title, c.course_code,
                   u.full_name as instructor_name
            FROM enrollments e
            JOIN courses c ON e.course_id = c.id
            JOIN users u ON c.instructor_id = u.id
            WHERE e.student_id = ?
            ORDER BY e.enrolled_at DESC
        ''', (current_user.id,)).fetchall()
        
        conn.close()
    
    return render_template('student/browse_courses.html', 
                         available_courses=available_courses,
                         my_enrollments=my_enrollments)

@app.route('/student/enroll', methods=['POST'])
@login_required
def student_enroll():
    """Enroll in a course with enrollment key"""
    if not current_user.is_student():
        flash('Access denied. Student account required.', 'error')
        return redirect(url_for('dashboard'))
    
    course_code = request.form.get('course_code', '').strip().upper()
    enrollment_key = request.form.get('enrollment_key', '').strip()
    
    if not course_code or not enrollment_key:
        flash('Course code and enrollment key are required.', 'error')
        return redirect(url_for('student_browse_courses'))
    
    with db_lock:
        conn = get_db_connection()
        
        # Find the course
        course = conn.execute('''
            SELECT c.*, u.full_name as instructor_name
            FROM courses c
            JOIN users u ON c.instructor_id = u.id
            WHERE c.course_code = ? AND c.is_active = 1
        ''', (course_code,)).fetchone()
        
        if not course:
            flash(f'Course with code "{course_code}" not found or is inactive.', 'error')
            conn.close()
            return redirect(url_for('student_browse_courses'))
        
        # Check if enrollment key is correct
        if not course['enrollment_key_hash'] or not check_password_hash(course['enrollment_key_hash'], enrollment_key):
            flash('Invalid enrollment key. Please check with your instructor.', 'error')
            conn.close()
            return redirect(url_for('student_browse_courses'))
        
        # Check if already enrolled
        existing_enrollment = conn.execute('''
            SELECT id FROM enrollments 
            WHERE student_id = ? AND course_id = ?
        ''', (current_user.id, course['id'])).fetchone()
        
        if existing_enrollment:
            flash('You have already requested enrollment for this course.', 'warning')
            conn.close()
            return redirect(url_for('student_browse_courses'))
        
        # Check course capacity
        current_enrollments = conn.execute('''
            SELECT COUNT(*) as count FROM enrollments 
            WHERE course_id = ? AND status = 'approved'
        ''', (course['id'],)).fetchone()
        
        if current_enrollments['count'] >= course['max_students']:
            flash('Course is full. No more enrollments accepted.', 'warning')
            conn.close()
            return redirect(url_for('student_browse_courses'))
        
        try:
            # Create enrollment request
            conn.execute('''
                INSERT INTO enrollments (student_id, course_id, status)
                VALUES (?, ?, 'pending')
            ''', (current_user.id, course['id']))
            
            # Create notification for the instructor
            conn.execute('''
                INSERT INTO notifications (user_id, title, message, type, related_id)
                VALUES (?, ?, ?, ?, ?)
            ''', (course['instructor_id'],
                  'New Enrollment Request',
                  f'{current_user.full_name} has requested enrollment in "{course["title"]}" ({course_code}). Please review and approve.',
                  'info',
                  course['id']))
            
            conn.commit()
            conn.close()
            
            flash(f'Enrollment request submitted for "{course["title"]}"! Your instructor ({course["instructor_name"]}) will review and approve your request.', 'success')
            
        except Exception as e:
            conn.rollback()
            conn.close()
            flash('Error submitting enrollment request. Please try again.', 'error')
    
    return redirect(url_for('student_browse_courses'))

@app.route('/student/enrollments')
@login_required
def student_my_enrollments():
    """View my enrollment status"""
    if not current_user.is_student():
        flash('Access denied. Student account required.', 'error')
        return redirect(url_for('dashboard'))
    
    with db_lock:
        conn = get_db_connection()
        
        enrollments = conn.execute('''
            SELECT e.*, c.title as course_title, c.course_code, c.description,
                   u.full_name as instructor_name
            FROM enrollments e
            JOIN courses c ON e.course_id = c.id
            JOIN users u ON c.instructor_id = u.id
            WHERE e.student_id = ?
            ORDER BY 
                CASE e.status 
                    WHEN 'pending' THEN 1
                    WHEN 'approved' THEN 2
                    WHEN 'rejected' THEN 3
                END,
                e.enrolled_at DESC
        ''', (current_user.id,)).fetchall()
        
        # Statistics
        stats = {
            'total_enrollments': len(enrollments),
            'pending_enrollments': len([e for e in enrollments if e['status'] == 'pending']),
            'approved_enrollments': len([e for e in enrollments if e['status'] == 'approved']),
            'rejected_enrollments': len([e for e in enrollments if e['status'] == 'rejected'])
        }
        
        conn.close()
    
    return render_template('student/my_enrollments.html', 
                         enrollments=enrollments, 
                         stats=stats)

# NEW FEATURE: Real-Time Discussion Forum (SMS-like messaging)
@app.route('/course/<int:course_id>/discussion')
@login_required
def course_discussion_forum(course_id):
    """Real-time discussion forum for course - SMS-like messaging between students and teachers"""
    with db_lock:
        conn = get_db_connection()
        
        # Verify user has access to this course
        course = conn.execute('SELECT * FROM courses WHERE id = ?', (course_id,)).fetchone()
        
        if not course:
            flash('Course not found.', 'error')
            conn.close()
            return redirect(url_for('dashboard'))
        
        # Check access
        access = conn.execute('''
            SELECT 1 FROM enrollments 
            WHERE student_id = ? AND course_id = ? AND status = 'approved'
        ''', (current_user.id, course_id)).fetchone()
        
        is_instructor = (current_user.id == course['instructor_id'])
        
        if not access and not is_instructor and not current_user.is_admin():
            flash('You do not have access to this course.', 'error')
            conn.close()
            return redirect(url_for('dashboard'))
        
        # Get existing messages
        messages = conn.execute('''
            SELECT cm.*, u.full_name as sender_name, u.role as sender_role
            FROM chat_messages cm
            JOIN users u ON cm.sender_id = u.id
            WHERE cm.course_id = ?
            ORDER BY cm.created_at ASC
        ''', (course_id,)).fetchall()
        
        conn.close()
    
    return render_template('course_discussion.html', course=course, messages=messages)

# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    return render_template('errors/404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template('errors/500.html'), 500

# SocketIO events for real-time chat
@socketio.on('connect')
def on_connect():
    if not current_user.is_authenticated:
        return False
    join_room(f'user_{current_user.id}')
    emit('status', {'msg': f'{current_user.full_name} has connected'})

# NOTIFICATIONS ROUTES
@app.route('/notifications')
@login_required
def notifications():
    """View all notifications for current user"""
    with db_lock:
        conn = get_db_connection()
        
        notifications = conn.execute('''
            SELECT * FROM notifications 
            WHERE user_id = ? 
            ORDER BY created_at DESC
        ''', (current_user.id,)).fetchall()
        
        # Mark all as read
        conn.execute('''
            UPDATE notifications 
            SET is_read = 1 
            WHERE user_id = ? AND is_read = 0
        ''', (current_user.id,))
        
        conn.commit()
        conn.close()
    
    return render_template('notifications.html', notifications=notifications)

@app.route('/api/notifications/unread-count')
@login_required
def get_unread_notifications_count():
    """Get count of unread notifications"""
    try:
        with db_lock:
            conn = get_db_connection()
            
            result = conn.execute('''
                SELECT COUNT(*) as count FROM notifications 
                WHERE user_id = ? AND is_read = 0
            ''', (current_user.id,)).fetchone()
            
            count = result['count'] if result else 0
            conn.close()
        
        return jsonify({'count': count})
    except Exception as e:
        print(f"Error getting unread notifications: {e}")
        return jsonify({'count': 0, 'error': str(e)}), 200

@app.route('/api/unread-messages/count')
@login_required
def get_unread_messages_count():
    """Get count of unread messages in all courses"""
    try:
        with db_lock:
            conn = get_db_connection()
            
            # Count unread messages in courses where user is enrolled
            result = conn.execute('''
                SELECT COUNT(*) as count FROM chat_messages cm
                JOIN enrollments e ON cm.course_id = e.course_id
                WHERE e.student_id = ? AND cm.sender_id != ? AND cm.created_at > (
                    SELECT COALESCE(MAX(viewed_at), '2020-01-01') FROM chat_messages
                    WHERE course_id = cm.course_id AND sender_id = ?
                )
            ''', (current_user.id, current_user.id, current_user.id)).fetchone()
            
            count = result['count'] if result else 0
            conn.close()
        
        return jsonify({'count': count})
    except Exception as e:
        print(f"Error getting unread messages: {e}")
        return jsonify({'count': 0, 'error': str(e)}), 200

@app.route('/api/generate-mcq-options', methods=['POST'])
@instructor_required
def api_generate_mcq_options():
    """API endpoint to generate MCQ options using AI"""
    data = request.get_json()
    question_text = data.get('question', '').strip()
    context = data.get('context', '').strip()
    
    if not question_text:
        return jsonify({'error': 'Question text is required'}), 400
    
    try:
        options = gemini_ai.generate_mcq_options(question_text, context)
        
        if options:
            return jsonify({
                'success': True,
                'options': options
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to generate options. Please try again.'
            }), 500
            
    except Exception as e:
        print(f"Error generating MCQ options: {e}")
        return jsonify({
            'success': False,
            'error': 'An error occurred while generating options'
        }), 500

@app.route('/api/submissions/<int:submission_id>')
@login_required
def api_get_submission(submission_id):
    """API endpoint to get detailed submission data for instructors"""
    with db_lock:
        conn = get_db_connection()
        
        # Get submission with student and quiz info
        submission = conn.execute('''
            SELECT s.*, u.full_name as student_name, u.username, u.email,
                   a.title as quiz_title, a.course_id, c.instructor_id
            FROM assignment_submissions s
            JOIN users u ON s.student_id = u.id
            JOIN assignments a ON s.assignment_id = a.id
            JOIN courses c ON a.course_id = c.id
            WHERE s.id = ?
        ''', (submission_id,)).fetchone()
        
        if not submission:
            conn.close()
            return jsonify({'error': 'Submission not found'}), 404
        
        # Check if user has access (instructor of the course)
        if not current_user.is_admin() and submission['instructor_id'] != current_user.id:
            conn.close()
            return jsonify({'error': 'Access denied'}), 403
        
        # Get MCQ answers
        answers = conn.execute('''
            SELECT ma.*, q.question_text, q.option_a, q.option_b, q.option_c, q.option_d,
                   q.correct_answer, q.points
            FROM student_mcq_answers ma
            JOIN quiz_questions q ON ma.question_id = q.id
            WHERE ma.submission_id = ?
            ORDER BY q.id
        ''', (submission_id,)).fetchall()
        
        conn.close()
        
        # Format response
        response = {
            'submission_id': submission['id'],
            'student_name': submission['student_name'],
            'username': submission['username'],
            'email': submission['email'],
            'quiz_title': submission['quiz_title'],
            'submitted_at': submission['submitted_at'],
            'grade': submission['grade'],
            'ai_feedback': submission['ai_feedback'],
            'answers': []
        }
        
        for answer in answers:
            response['answers'].append({
                'question_id': answer['question_id'],
                'question_text': answer['question_text'],
                'options': {
                    'A': answer['option_a'],
                    'B': answer['option_b'],
                    'C': answer['option_c'],
                    'D': answer['option_d']
                },
                'selected_option': answer['selected_option'],
                'correct_answer': answer['correct_answer'],
                'is_correct': answer['is_correct'],
                'points_earned': answer['points_earned'],
                'points_possible': answer['points']
            })
        
        return jsonify(response)

@app.route('/notifications/<int:notification_id>/mark-read', methods=['POST'])
@login_required  
def mark_notification_read(notification_id):
    """Mark specific notification as read"""
    with db_lock:
        conn = get_db_connection()
        
        conn.execute('''
            UPDATE notifications 
            SET is_read = 1 
            WHERE id = ? AND user_id = ?
        ''', (notification_id, current_user.id))
        
        conn.commit()
        conn.close()
    
    return jsonify({'success': True})

@app.route('/notifications/<int:notification_id>/redirect')
@login_required
def handle_notification_redirect(notification_id):
    """Redirect user to relevant page based on notification type and mark as read"""
    with db_lock:
        conn = get_db_connection()
        notification = conn.execute('''
            SELECT * FROM notifications WHERE id = ? AND user_id = ?
        ''', (notification_id, current_user.id)).fetchone()
        
        if notification:
            conn.execute('UPDATE notifications SET is_read = 1 WHERE id = ?', (notification_id,))
            conn.commit()
        
        conn.close()
    
    if not notification:
        return redirect(url_for('dashboard'))
    
    # Route based on notification type and content
    message_lower = notification['message'].lower()
    course_id = notification['related_id']
    
    # Check what type of notification this is
    if 'forum' in message_lower or 'discussion' in message_lower or 'topic' in message_lower:
        # Redirect to course forums if we have a course ID
        if course_id:
            return redirect(url_for('student_course_forums', course_id=course_id))
        else:
            return redirect(url_for('dashboard'))
    
    elif 'message' in message_lower or 'chat' in message_lower or 'community' in message_lower:
        # Redirect to course discussion
        if course_id:
            return redirect(url_for('course_discussion_forum', course_id=course_id))
        else:
            return redirect(url_for('dashboard'))
    
    elif 'quiz' in message_lower or 'assignment' in message_lower or 'grade' in message_lower:
        # Redirect to course page for quiz/assignment notifications
        if course_id:
            return redirect(url_for('student_course_view', course_id=course_id))
        else:
            return redirect(url_for('dashboard'))
    
    elif 'enrollment' in message_lower or 'course' in message_lower or 'approved' in message_lower:
        # Redirect to course if available
        if course_id:
            return redirect(url_for('student_course_view', course_id=course_id))
        else:
            return redirect(url_for('dashboard'))
    
    elif 'progress' in message_lower:
        # Redirect to course for progress updates
        if course_id:
            return redirect(url_for('student_course_view', course_id=course_id))
        else:
            return redirect(url_for('dashboard'))
    
    # Default fallback
    if course_id:
        return redirect(url_for('student_course_view', course_id=course_id))
    
    return redirect(url_for('dashboard'))

# API ENDPOINTS FOR FETCHING MESSAGES
@app.route('/api/course/<int:course_id>/messages')
@login_required
def get_course_messages(course_id):
    """Get all messages for a course"""
    try:
        with db_lock:
            conn = get_db_connection()
            
            # Verify access
            access = conn.execute('''
                SELECT 1 FROM enrollments 
                WHERE student_id = ? AND course_id = ? AND status = 'approved'
            ''', (current_user.id, course_id)).fetchone()
            
            is_instructor = conn.execute('''
                SELECT 1 FROM courses WHERE id = ? AND instructor_id = ?
            ''', (course_id, current_user.id)).fetchone()
            
            if not (access or is_instructor or current_user.is_admin()):
                conn.close()
                return jsonify({'error': 'Access denied'}), 403
            
            messages = conn.execute('''
                SELECT cm.*, u.full_name, u.role 
                FROM chat_messages cm
                JOIN users u ON cm.sender_id = u.id
                WHERE cm.course_id = ?
                ORDER BY cm.created_at ASC
            ''', (course_id,)).fetchall()
            
            conn.close()
        
        return jsonify({
            'success': True,
            'messages': [dict(m) for m in messages] if messages else []
        })
    except Exception as e:
        print(f"Error fetching course messages: {e}")
        return jsonify({'success': False, 'messages': [], 'error': str(e)}), 200

# SOCKETIO CHAT HANDLERS
@socketio.on('disconnect')
def on_disconnect():
    if current_user.is_authenticated:
        leave_room(f'user_{current_user.id}')

@socketio.on('join_course_chat')
def on_join_course(data):
    if not current_user.is_authenticated:
        return
    
    course_id = data['course_id']
    
    # Verify user has access to this course
    with db_lock:
        conn = get_db_connection()
        access = conn.execute('''
            SELECT 1 FROM enrollments e
            JOIN courses c ON e.course_id = c.id
            WHERE (e.student_id = ? OR c.instructor_id = ?) AND c.id = ? AND e.status = 'approved'
        ''', (current_user.id, current_user.id, course_id)).fetchone()
        conn.close()
    
    if access or current_user.is_admin():
        join_room(f'course_{course_id}')
        socketio.emit('status', {'msg': f'{current_user.full_name} joined the course chat'}, to=f'course_{course_id}')

@socketio.on('send_course_message')
def handle_course_message(data):
    if not current_user.is_authenticated:
        return
    
    course_id = data['course_id']
    message = data['message'].strip()
    sender_role = data.get('sender_role', current_user.role)
    
    if not message:
        return
    
    # Save message to database
    with db_lock:
        conn = get_db_connection()
        
        # Insert message
        conn.execute('''
            INSERT INTO chat_messages (course_id, sender_id, message)
            VALUES (?, ?, ?)
        ''', (course_id, current_user.id, message))
        
        # Get all students in the course to send notifications
        students = conn.execute('''
            SELECT student_id FROM enrollments 
            WHERE course_id = ? AND status = 'approved' AND student_id != ?
        ''', (course_id, current_user.id)).fetchall()
        
        conn.commit()
        conn.close()
    
    # Emit message to course room
    socketio.emit('new_course_message', {
        'message': message,
        'sender_name': current_user.full_name,
        'sender_id': current_user.id,
        'sender_role': sender_role,
        'course_id': course_id,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }, to=f'course_{course_id}')
    
    # Send notifications to all other students in the course
    for student in students:
        socketio.emit('new_message_notification', {
            'title': f'💬 New Message in Course',
            'message': f'{current_user.full_name}: {message[:50]}{"..." if len(message) > 50 else ""}',
            'type': 'info',
            'icon': 'fa-comments',
            'course_id': course_id,
            'action_url': f'/discussions/{course_id}'
        }, to=f'user_{student["student_id"]}')

# COMMUNITY CHAT HANDLERS
@socketio.on('join_community_chat')
def on_join_community(data):
    if not current_user.is_authenticated:
        return
    join_room('community_chat')

@socketio.on('send_community_message')
def handle_community_message(data):
    if not current_user.is_authenticated:
        return
    
    message = data.get('message', '').strip()
    sender_role = data.get('sender_role', current_user.role)
    file_path = data.get('file_path')
    file_name = data.get('file_name')
    is_image = data.get('is_image', 0)
    is_video = data.get('is_video', 0)
    profile_picture = data.get('profile_picture', current_user.profile_picture)
    
    if not message:
        return
    
    # Save to database
    message_id = None
    with db_lock:
        conn = get_db_connection()
        cursor = conn.execute('''
            INSERT INTO chat_messages (course_id, sender_id, message, message_type, file_path, file_name, is_image)
            VALUES (NULL, ?, ?, ?, ?, ?, ?)
        ''', (current_user.id, message, 'file' if file_path else 'text', file_path, file_name, is_image or is_video))
        message_id = cursor.lastrowid
        conn.commit()
        conn.close()
    
    socketio.emit('new_community_message', {
        'id': message_id,
        'message': message,
        'sender_name': current_user.full_name,
        'sender_id': current_user.id,
        'sender_role': sender_role,
        'profile_picture': profile_picture,
        'file_path': file_path,
        'file_name': file_name,
        'is_image': is_image,
        'is_video': is_video,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }, to='community_chat')

# DIRECT MESSAGE HANDLERS (Admin/Instructor Chat)
@socketio.on('join_direct_chat')
def on_join_direct_chat(data):
    if not current_user.is_authenticated:
        return
    
    recipient_id = data.get('recipient_id')
    room = f'direct_{min(current_user.id, recipient_id)}_{max(current_user.id, recipient_id)}'
    join_room(room)

@socketio.on('send_direct_message')
def handle_direct_message(data):
    if not current_user.is_authenticated:
        return
    
    recipient_id = data.get('recipient_id')
    message = data.get('message', '').strip()
    
    if not message or not recipient_id:
        return
    
    # Save to database
    with db_lock:
        conn = get_db_connection()
        conn.execute('''
            INSERT INTO direct_messages (sender_id, recipient_id, message)
            VALUES (?, ?, ?)
        ''', (current_user.id, recipient_id, message))
        conn.commit()
        conn.close()
    
    room = f'direct_{min(current_user.id, recipient_id)}_{max(current_user.id, recipient_id)}'
    socketio.emit('new_direct_message', {
        'sender_name': current_user.full_name,
        'sender_id': current_user.id,
        'message': message,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }, to=room)
    
    # Send notification to recipient
    socketio.emit('direct_message_notification', {
        'title': f'💌 Direct Message from {current_user.full_name}',
        'message': message[:50],
        'type': 'warning',
        'icon': 'fa-envelope'
    }, to=f'user_{recipient_id}')

# FILE UPLOAD HANDLER
@app.route('/api/upload-chat-file', methods=['POST'])
@login_required
def upload_chat_file():
    """Upload file to chat"""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        
        file = request.files['file']
        course_id = request.form.get('course_id')
        message_text = request.form.get('message', '')
        
        if not file.filename:
            return jsonify({'error': 'No file selected'}), 400
        
        # Check file extension
        file_ext = file.filename.rsplit('.', 1)[-1].lower()
        if file_ext not in ALLOWED_FILE_TYPES:
            return jsonify({'error': f'File type .{file_ext} not allowed'}), 400
        
        # Check file size
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)
        
        if file_size > MAX_CHAT_FILE_SIZE:
            return jsonify({'error': f'File size exceeds {MAX_CHAT_FILE_SIZE / (1024*1024):.0f} MB limit'}), 400
        
        # Check user storage
        with db_lock:
            conn = get_db_connection()
            user_storage = conn.execute('''
                SELECT SUM(file_size) as total FROM file_uploads 
                WHERE uploader_id = ?
            ''', (current_user.id,)).fetchone()
            total_used = user_storage['total'] or 0
            conn.close()
        
        if total_used + file_size > MAX_TOTAL_STORAGE_PER_USER:
            return jsonify({'error': 'Storage limit exceeded (5 GB per user)'}), 400
        
        # Save file
        unique_filename = f"{current_user.id}_{uuid.uuid4().hex}_{secure_filename(file.filename)}"
        original_filename = file.filename
        upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'chat_files')
        filepath = os.path.join(upload_dir, unique_filename)
        file.save(filepath)
        
        # Determine if image
        is_image = file_ext in {'jpg', 'jpeg', 'png', 'gif'}
        
        # Save to database
        with db_lock:
            conn = get_db_connection()
            conn.execute('''
                INSERT INTO chat_messages 
                (course_id, sender_id, message, message_type, file_path, file_name, file_size, is_image)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (course_id, current_user.id, message_text or original_filename, 'file', unique_filename, original_filename, file_size, is_image))
            
            message_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            
            conn.execute('''
                INSERT INTO file_uploads (uploader_id, file_name, file_path, file_size, file_type, message_id)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (current_user.id, original_filename, unique_filename, file_size, file_ext, message_id))
            
            conn.commit()
            conn.close()
        
        return jsonify({
            'success': True,
            'file_path': f'/uploads/chat_files/{unique_filename}',
            'file_name': original_filename,
            'file_type': file_ext
        })
        
    except Exception as e:
        logging.error(f"File upload error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/download-chat-file/<filename>')
@login_required
def download_chat_file(filename):
    """Download uploaded chat file"""
    try:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], 'chat_files', filename)
        if os.path.exists(filepath):
            return send_from_directory(os.path.dirname(filepath), filename, as_attachment=True)
        return jsonify({'error': 'File not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/uploads/chat_files/<filename>')
@login_required
def serve_chat_file(filename):
    """Serve uploaded chat files"""
    return send_from_directory(os.path.join(app.config['UPLOAD_FOLDER'], 'chat_files'), filename)

# MESSAGING HUB ROUTE
@app.route('/forum')
@login_required
def messaging_hub():
    """Central messaging hub showing all chat options"""
    with db_lock:
        conn = get_db_connection()
        
        # Get user's courses if student
        my_courses = []
        if current_user.role == 'student':
            my_courses = conn.execute('''
                SELECT c.id, c.title, c.course_code 
                FROM courses c
                JOIN enrollments e ON c.id = e.course_id
                WHERE e.student_id = ? AND e.status = 'approved'
                ORDER BY c.title
            ''', (current_user.id,)).fetchall()
        
        # Get instructor's courses
        instructor_courses = []
        if current_user.is_instructor() or current_user.is_admin():
            instructor_courses = conn.execute('''
                SELECT id, title, course_code FROM courses 
                WHERE instructor_id = ?
                ORDER BY title
            ''', (current_user.id,)).fetchall()
        
        # Get all courses for admin
        all_courses = []
        if current_user.is_admin():
            all_courses = conn.execute('''
                SELECT c.id, c.title, c.course_code, u.full_name as instructor_name
                FROM courses c
                LEFT JOIN users u ON c.instructor_id = u.id
                ORDER BY c.title
            ''', ).fetchall()
        
        conn.close()
    
    return render_template('messaging_hub.html', 
                         my_courses=my_courses,
                         instructor_courses=instructor_courses,
                         all_courses=all_courses)

# COMMUNITY CHAT ROUTE
@app.route('/community-chat')
@login_required
def community_chat():
    """Display community chat (all users)"""
    return render_template('community_chat.html')

# DIRECT MESSAGES CRUD ROUTES
@app.route('/direct-messages')
@login_required
def direct_messages_page():
    """Display direct messages page"""
    with db_lock:
        conn = get_db_connection()
        # Get all students for instructor/admin, or instructors for students
        if current_user.is_admin() or current_user.is_instructor():
            users = conn.execute('''
                SELECT DISTINCT u.id, u.full_name, u.role FROM users u
                WHERE u.role = 'student' AND u.id != ?
                ORDER BY u.full_name
            ''', (current_user.id,)).fetchall()
        else:
            users = conn.execute('''
                SELECT DISTINCT u.id, u.full_name, u.role FROM users u
                WHERE (u.role = 'instructor' OR u.role = 'admin') AND u.id != ?
                ORDER BY u.full_name
            ''', (current_user.id,)).fetchall()
        conn.close()
    
    return render_template('direct_messages.html', users=users)

@app.route('/api/direct-messages/<int:user_id>')
@login_required
def get_direct_messages(user_id):
    """Get direct messages with a specific user"""
    try:
        with db_lock:
            conn = get_db_connection()
            messages = conn.execute('''
                SELECT id, sender_id, recipient_id, message, created_at 
                FROM direct_messages 
                WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?)
                ORDER BY created_at ASC
                LIMIT 100
            ''', (current_user.id, user_id, user_id, current_user.id)).fetchall()
            
            # Mark as read
            conn.execute('''
                UPDATE direct_messages SET is_read = 1 
                WHERE recipient_id = ? AND sender_id = ? AND is_read = 0
            ''', (current_user.id, user_id))
            conn.commit()
            conn.close()
        
        return jsonify({'messages': [dict(m) for m in messages]})
    except Exception as e:
        print(f"Error loading direct messages: {e}")
        return jsonify({'messages': [], 'error': str(e)}), 200

@app.route('/api/direct-messages/<int:message_id>', methods=['DELETE'])
@login_required
def delete_direct_message(message_id):
    """Delete a direct message"""
    try:
        with db_lock:
            conn = get_db_connection()
            # Verify ownership
            msg = conn.execute('SELECT sender_id FROM direct_messages WHERE id = ?', (message_id,)).fetchone()
            
            if not msg or msg['sender_id'] != current_user.id:
                conn.close()
                return jsonify({'error': 'Unauthorized'}), 403
            
            conn.execute('DELETE FROM direct_messages WHERE id = ?', (message_id,))
            conn.commit()
            conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        print(f"Error deleting message: {e}")
        return jsonify({'success': False, 'error': str(e)}), 200

@app.route('/api/direct-messages/<int:message_id>', methods=['PATCH'])
@login_required
def edit_direct_message(message_id):
    """Edit a direct message"""
    try:
        data = request.get_json()
        message = data.get('message', '').strip() if data else ''
        
        if not message:
            return jsonify({'error': 'Message cannot be empty'}), 400
        
        with db_lock:
            conn = get_db_connection()
            # Verify ownership
            msg = conn.execute('SELECT sender_id FROM direct_messages WHERE id = ?', (message_id,)).fetchone()
            
            if not msg or msg['sender_id'] != current_user.id:
                conn.close()
                return jsonify({'error': 'Unauthorized'}), 403
            
            conn.execute('UPDATE direct_messages SET message = ? WHERE id = ?', (message, message_id))
            conn.commit()
            conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        print(f"Error editing message: {e}")
        return jsonify({'success': False, 'error': str(e)}), 200

# USER PROFILE ENDPOINTS
@app.route('/api/profile/bio', methods=['POST'])
@login_required
def update_bio():
    """Update user bio"""
    try:
        data = request.get_json()
        bio = data.get('bio', '').strip()[:500]
        
        with db_lock:
            conn = get_db_connection()
            conn.execute('UPDATE users SET bio = ? WHERE id = ?', (bio, current_user.id))
            conn.commit()
            conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 200

@app.route('/api/profile/phone', methods=['POST'])
@login_required
def update_phone():
    """Update phone number"""
    try:
        data = request.get_json()
        phone = data.get('phone', '').strip()
        
        with db_lock:
            conn = get_db_connection()
            conn.execute('''INSERT OR REPLACE INTO user_profiles (user_id, phone_number)
                VALUES (?, ?)''', (current_user.id, phone))
            conn.commit()
            conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 200

@app.route('/api/contacts')
@login_required
def get_contacts():
    """Get user contacts"""
    try:
        with db_lock:
            conn = get_db_connection()
            contacts = conn.execute('''SELECT u.id, u.full_name FROM contacts c
                JOIN users u ON c.contact_id = u.id WHERE c.user_id = ?
                ORDER BY u.full_name''', (current_user.id,)).fetchall()
            conn.close()
        
        return jsonify({'contacts': [dict(c) for c in contacts]})
    except Exception as e:
        return jsonify({'contacts': [], 'error': str(e)}), 200

@app.route('/api/messages/<int:message_id>/react', methods=['POST'])
@login_required
def add_reaction(message_id):
    """Add emoji reaction to message"""
    try:
        data = request.get_json()
        emoji = data.get('emoji', '').strip()[:2]
        
        if not emoji:
            return jsonify({'error': 'Invalid emoji'}), 400
        
        with db_lock:
            conn = get_db_connection()
            conn.execute('''INSERT OR IGNORE INTO message_reactions (message_id, user_id, emoji)
                VALUES (?, ?, ?)''', (message_id, current_user.id, emoji))
            conn.commit()
            conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False}), 200

# CRUD OPERATIONS FOR CHAT MESSAGES
@app.route('/api/chat-messages/<int:message_id>', methods=['PATCH'])
@login_required
def edit_chat_message(message_id):
    """Edit a chat message (community or course)"""
    try:
        data = request.get_json()
        message = data.get('message', '').strip()
        
        if not message:
            return jsonify({'error': 'Message cannot be empty'}), 400
        
        with db_lock:
            conn = get_db_connection()
            # Verify ownership
            msg = conn.execute('SELECT sender_id FROM chat_messages WHERE id = ?', (message_id,)).fetchone()
            
            if not msg or msg['sender_id'] != current_user.id:
                conn.close()
                return jsonify({'error': 'Unauthorized'}), 403
            
            conn.execute('UPDATE chat_messages SET message = ? WHERE id = ?', (message, message_id))
            conn.commit()
            conn.close()
        
        return jsonify({'success': True, 'message': message})
    except Exception as e:
        logging.error(f"Error editing message: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/chat-messages/<int:message_id>', methods=['DELETE'])
@login_required
def delete_chat_message(message_id):
    """Delete a chat message (community or course)"""
    try:
        with db_lock:
            conn = get_db_connection()
            # Verify ownership
            msg = conn.execute('SELECT sender_id FROM chat_messages WHERE id = ?', (message_id,)).fetchone()
            
            if not msg or msg['sender_id'] != current_user.id:
                conn.close()
                return jsonify({'error': 'Unauthorized'}), 403
            
            conn.execute('DELETE FROM chat_messages WHERE id = ?', (message_id,))
            conn.commit()
            conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error deleting message: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@socketio.on('user_typing')
def handle_typing(data):
    """Broadcast typing indicator"""
    if not current_user.is_authenticated:
        return
    
    socketio.emit('user_typing', {
        'user_id': current_user.id,
        'user_name': current_user.full_name,
        'room': data.get('room')
    }, to=data.get('room'))

@socketio.on('update_status')
def update_status(data):
    """Update user online/offline status"""
    if not current_user.is_authenticated:
        return
    
    with db_lock:
        conn = get_db_connection()
        status = data.get('status', 'online')
        conn.execute('''INSERT OR REPLACE INTO user_profiles (user_id, status, last_seen)
            VALUES (?, ?, CURRENT_TIMESTAMP)''', (current_user.id, status))
        conn.commit()
        conn.close()
    
    socketio.emit('user_status_changed', {
        'user_id': current_user.id,
        'status': status
    }, broadcast=True)


@app.route('/course/<int:course_id>/ai_notes')
@login_required
def ai_notes_page(course_id):
    """AI Notes page for instructors and students"""
    with db_lock:
        conn = get_db_connection()
        course = conn.execute('SELECT * FROM courses WHERE id = ?', (course_id,)).fetchone()
        
        if not course:
            conn.close()
            flash('Course not found', 'error')
            return redirect(url_for('dashboard'))
        
        if current_user.role == 'instructor':
            if course['instructor_id'] != current_user.id:
                conn.close()
                flash('You do not have permission to access this course', 'error')
                return redirect(url_for('dashboard'))
            
            my_notes = conn.execute('''SELECT * FROM ai_notes WHERE course_id = ? AND created_by = ? 
                AND is_instructor_note = 1 ORDER BY created_at DESC''', 
                (course_id, current_user.id)).fetchall()
            conn.close()
            return render_template('ai_notes_instructor.html', course=course, notes=my_notes)
        else:
            enrollment = conn.execute('''SELECT * FROM enrollments WHERE course_id = ? AND student_id = ? 
                AND status = 'approved' ''', (course_id, current_user.id)).fetchone()
            
            if not enrollment:
                conn.close()
                flash('You are not enrolled in this course', 'error')
                return redirect(url_for('dashboard'))
            
            instructor_notes = conn.execute('''SELECT an.*, u.full_name as instructor_name FROM ai_notes an 
                JOIN users u ON an.created_by = u.id 
                WHERE an.course_id = ? AND an.is_instructor_note = 1 AND an.sent_to_students = 1 
                ORDER BY an.created_at DESC''', (course_id,)).fetchall()
            my_notes = conn.execute('''SELECT * FROM ai_notes WHERE course_id = ? AND created_by = ? 
                AND is_instructor_note = 0 ORDER BY created_at DESC''', 
                (course_id, current_user.id)).fetchall()
            conn.close()
            return render_template('ai_notes_student.html', course=course, 
                instructor_notes=instructor_notes, my_notes=my_notes)


@app.route('/course/<int:course_id>/ai_notes/generate', methods=['POST'])
@login_required
@instructor_required
def generate_ai_notes(course_id):
    """Generate AI notes for a topic"""
    try:
        with db_lock:
            conn = get_db_connection()
            course = conn.execute('SELECT * FROM courses WHERE id = ? AND instructor_id = ?', 
                (course_id, current_user.id)).fetchone()
            conn.close()
        
        if not course:
            return jsonify({'success': False, 'error': 'You do not own this course'}), 403
        
        topic = request.form.get('topic', '').strip()
        additional_context = request.form.get('additional_context', '').strip()
        
        if not topic:
            return jsonify({'success': False, 'error': 'Topic is required'}), 400
        
        result = gemini_ai.generate_ai_notes(topic, current_user.full_name, additional_context)
        
        if result.get('success'):
            with db_lock:
                conn = get_db_connection()
                conn.execute('''INSERT INTO ai_notes (course_id, topic, content, created_by, is_instructor_note)
                    VALUES (?, ?, ?, ?, 1)''', (course_id, topic, result['content'], current_user.id))
                note_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                conn.commit()
                conn.close()
            
            return jsonify({'success': True, 'note_id': note_id, 'content': result['content']})
        else:
            return jsonify({'success': False, 'error': result.get('error', 'Failed to generate notes')}), 500
    
    except Exception as e:
        logging.error(f"Error generating AI notes: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/course/<int:course_id>/ai_notes/<int:note_id>/edit', methods=['POST'])
@login_required
@instructor_required
def edit_ai_notes(course_id, note_id):
    """Edit AI notes content"""
    try:
        content = request.form.get('content', '').strip()
        
        if not content:
            return jsonify({'success': False, 'error': 'Content is required'}), 400
        
        with db_lock:
            conn = get_db_connection()
            conn.execute('UPDATE ai_notes SET content = ? WHERE id = ? AND created_by = ? AND course_id = ?',
                (content, note_id, current_user.id, course_id))
            conn.commit()
            conn.close()
        
        return jsonify({'success': True})
    
    except Exception as e:
        logging.error(f"Error editing AI notes: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/course/<int:course_id>/ai_notes/<int:note_id>/create_pdf', methods=['POST'])
@login_required
@instructor_required
def create_notes_pdf(course_id, note_id):
    """Create PDF from AI notes"""
    try:
        from utils.pdf_generator import generate_notes_pdf
        
        with db_lock:
            conn = get_db_connection()
            note = conn.execute('SELECT * FROM ai_notes WHERE id = ? AND created_by = ? AND course_id = ?',
                (note_id, current_user.id, course_id)).fetchone()
            conn.close()
        
        if not note:
            return jsonify({'success': False, 'error': 'Note not found'}), 404
        
        notes_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'ai_notes')
        os.makedirs(notes_dir, exist_ok=True)
        
        filename = f'notes_{note_id}_{int(datetime.now().timestamp())}.pdf'
        pdf_path = os.path.join(notes_dir, filename)
        
        success = generate_notes_pdf(note['content'], note['topic'], current_user.full_name, pdf_path)
        
        if success:
            relative_path = f'ai_notes/{filename}'
            
            with db_lock:
                conn = get_db_connection()
                conn.execute('UPDATE ai_notes SET pdf_path = ? WHERE id = ?', (relative_path, note_id))
                conn.commit()
                conn.close()
            
            return jsonify({'success': True, 'pdf_path': relative_path})
        else:
            return jsonify({'success': False, 'error': 'Failed to create PDF'}), 500
    
    except Exception as e:
        logging.error(f"Error creating PDF: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/course/<int:course_id>/ai_notes/<int:note_id>/send', methods=['POST'])
@login_required
@instructor_required
def send_notes_to_students(course_id, note_id):
    """Send AI notes to all enrolled students"""
    try:
        with db_lock:
            conn = get_db_connection()
            note = conn.execute('SELECT * FROM ai_notes WHERE id = ? AND created_by = ? AND course_id = ?',
                (note_id, current_user.id, course_id)).fetchone()
            
            if not note:
                conn.close()
                return jsonify({'success': False, 'error': 'Note not found'}), 404
            
            if not note['pdf_path']:
                conn.close()
                return jsonify({'success': False, 'error': 'Please create PDF first'}), 400
            
            conn.execute('UPDATE ai_notes SET sent_to_students = 1 WHERE id = ?', (note_id,))
            
            students = conn.execute('''SELECT student_id FROM enrollments WHERE course_id = ? 
                AND status = 'approved' ''', (course_id,)).fetchall()
            
            for student in students:
                conn.execute('''INSERT INTO notifications (user_id, title, message, type, related_id)
                    VALUES (?, ?, ?, ?, ?)''',
                    (student['student_id'], 'New AI Notes Available', 
                    f'New AI notes available for {note["topic"]}', 'ai_notes', note_id))
            
            conn.commit()
            conn.close()
        
        return jsonify({'success': True})
    
    except Exception as e:
        logging.error(f"Error sending notes: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/course/<int:course_id>/ai_notes/<int:note_id>/delete', methods=['POST'])
@login_required
def delete_ai_notes(course_id, note_id):
    """Delete AI notes"""
    try:
        with db_lock:
            conn = get_db_connection()
            note = conn.execute('SELECT * FROM ai_notes WHERE id = ? AND created_by = ? AND course_id = ?',
                (note_id, current_user.id, course_id)).fetchone()
            
            if not note:
                conn.close()
                return jsonify({'success': False, 'error': 'Note not found'}), 404
            
            # Delete PDF file if it exists
            if note['pdf_path']:
                pdf_file = os.path.join(app.config['UPLOAD_FOLDER'], note['pdf_path'])
                if os.path.exists(pdf_file):
                    try:
                        os.remove(pdf_file)
                    except Exception as e:
                        logging.warning(f"Could not delete PDF file: {e}")
            
            conn.execute('DELETE FROM ai_notes WHERE id = ?', (note_id,))
            conn.commit()
            conn.close()
        
        return jsonify({'success': True})
    
    except Exception as e:
        logging.error(f"Error deleting AI notes: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/course/<int:course_id>/ai_notes/student/create', methods=['POST'])
@login_required
def student_create_notes(course_id):
    """Student create their own AI notes"""
    try:
        with db_lock:
            conn = get_db_connection()
            enrollment = conn.execute('''SELECT * FROM enrollments WHERE course_id = ? AND student_id = ? 
                AND status = 'approved' ''', (course_id, current_user.id)).fetchone()
            conn.close()
        
        if not enrollment:
            return jsonify({'success': False, 'error': 'You are not enrolled in this course'}), 403
        
        topic = request.form.get('topic', '').strip()
        additional_context = request.form.get('additional_context', '').strip()
        add_watermark = request.form.get('add_watermark') == 'on'
        custom_watermark = request.form.get('custom_watermark', '').strip()
        
        if not topic:
            return jsonify({'success': False, 'error': 'Topic is required'}), 400
        
        result = gemini_ai.generate_ai_notes(topic, '', additional_context)
        
        if result.get('success'):
            from utils.pdf_generator import generate_notes_pdf
            
            with db_lock:
                conn = get_db_connection()
                conn.execute('''INSERT INTO ai_notes (course_id, topic, content, created_by, is_instructor_note)
                    VALUES (?, ?, ?, ?, 0)''', (course_id, topic, result['content'], current_user.id))
                note_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                conn.commit()
                conn.close()
            
            notes_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'ai_notes')
            os.makedirs(notes_dir, exist_ok=True)
            
            filename = f'notes_{note_id}_{int(datetime.now().timestamp())}.pdf'
            pdf_path = os.path.join(notes_dir, filename)
            
            success = generate_notes_pdf(result['content'], topic, current_user.full_name, pdf_path, 
                                        add_watermark=add_watermark, custom_watermark=custom_watermark)
            
            relative_path = None
            if success:
                relative_path = f'ai_notes/{filename}'
                
                with db_lock:
                    conn = get_db_connection()
                    conn.execute('UPDATE ai_notes SET pdf_path = ? WHERE id = ?', (relative_path, note_id))
                    conn.commit()
                    conn.close()
            
            return jsonify({'success': True, 'note_id': note_id, 'pdf_path': relative_path})
        else:
            return jsonify({'success': False, 'error': result.get('error', 'Failed to generate notes')}), 500
    
    except Exception as e:
        logging.error(f"Error creating student notes: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    """Serve uploaded files"""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


if __name__ == '__main__':
    # Initialize database before starting Flask
    db_ready = init_db()
    if not db_ready:
        print("⚠️  Database initialization incomplete, but continuing with Flask startup...")
    
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, use_reloader=False, log_output=True, allow_unsafe_werkzeug=True)