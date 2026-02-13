<script>
function enrollInCourse(courseCode, courseTitle) {
    document.getElementById('modalCourseTitle').textContent = courseTitle;
    document.getElementById('modalCourseCode').textContent = courseCode;
    document.getElementById('modalCourseCodeInput').value = courseCode;
    document.getElementById('modalEnrollmentKey').value = '';
    document.getElementById('enrollmentModal').style.display = 'block';
    document.getElementById('modalEnrollmentKey').focus();
}

function closeEnrollmentModal() {
    document.getElementById('enrollmentModal').style.display = 'none';
}

function viewCourseDetails(courseId) {
    window.location.href = '{{ url_for("student_browse_courses") }}#course-' + courseId;
}

// Set current date
document.addEventListener('DOMContentLoaded', function() {
    const now = new Date();
    const options = { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' };
    document.getElementById('current-date').textContent = now.toLocaleDateString('en-US', options);
    
    // Initialize progress animations
    initializeProgressAnimations();
});

// Function to initialize progress animations
function initializeProgressAnimations() {
    const progressBars = document.querySelectorAll('.progress-fill');
    
    progressBars.forEach(bar => {
        // Add a slight delay to make the animation more noticeable
        setTimeout(() => {
            const width = bar.style.width;
            bar.style.width = '0%';
            
            // Animate to the actual width
            setTimeout(() => {
                bar.style.width = width;
            }, 300);
        }, 500);
    });
}

// Function to simulate progress update
function simulateProgressUpdate(courseCode, newProgress) {
    const courseItems = document.querySelectorAll('.course-item');
    
    courseItems.forEach(item => {
        const codeElement = item.querySelector('.course-code');
        if (codeElement && codeElement.textContent === courseCode) {
            const progressFill = item.querySelector('.progress-fill');
            const progressPercentage = item.querySelector('.progress-percentage');
            
            if (progressFill) {
                progressFill.style.width = `${newProgress}%`;
                
                // Add a glow effect
                progressFill.style.animation = 'progress-glow 1s ease';
                
                // Remove animation after it completes
                setTimeout(() => {
                    progressFill.style.animation = '';
                }, 1000);
            }
        }
    });
}

// Close modal when clicking outside
window.addEventListener('click', function(event) {
    const modal = document.getElementById('enrollmentModal');
    if (event.target === modal) {
        closeEnrollmentModal();
    }
});

// Close modal on escape key
document.addEventListener('keydown', function(event) {
    if (event.key === 'Escape') {
        closeEnrollmentModal();
    }
});

// Demo function to show progress animation (remove in production)
function demoProgressAnimation() {
    setTimeout(() => {
        simulateProgressUpdate('CS101', 75);
    }, 2000);
    
    setTimeout(() => {
        simulateProgressUpdate('MATH201', 45);
    }, 4000);
}

// Initialize demo on page load (remove in production)
document.addEventListener('DOMContentLoaded', function() {
    // Uncomment the line below to see demo progress animations
    // demoProgressAnimation();
});
</script>
