// Main JavaScript for LearnNest LMS
// Mobile menu is handled in base.html to avoid conflicts

// Flash message auto-dismiss
document.addEventListener('DOMContentLoaded', function() {
    const flashMessages = document.querySelectorAll('.flash-message');
    
    flashMessages.forEach(function(message) {
        setTimeout(function() {
            message.style.opacity = '0';
            message.style.transform = 'translateX(100%)';
            setTimeout(function() {
                message.remove();
            }, 300);
        }, 5000);
    });
});

// Utility function for confirming destructive actions
function confirmAction(message) {
    return confirm(message || 'Are you sure you want to proceed?');
}

// Copy to clipboard utility
function copyToClipboard(text) {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
    document.body.removeChild(textarea);
    
    // Show feedback
    const feedback = document.createElement('div');
    feedback.textContent = 'Copied to clipboard!';
    feedback.className = 'flash-message success';
    feedback.style.position = 'fixed';
    feedback.style.top = '20px';
    feedback.style.right = '20px';
    feedback.style.zIndex = '10000';
    document.body.appendChild(feedback);
    
    setTimeout(function() {
        feedback.remove();
    }, 2000);
}
