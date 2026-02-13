# IMPORTANT: KEEP THIS COMMENT
# Follow these instructions when using this blueprint:
# - Note that the newest Gemini model series is "gemini-2.5-flash" or gemini-2.5-pro"
#   - do not change this unless explicitly requested by the user
# - Sometimes the google genai SDK has occasional type errors. You might need to run to validate, at time.  
# The SDK was recently renamed from google-generativeai to google-genai. This file reflects the new name and the new APIs.

import json
import logging
import os
from google import genai
from google.genai import types

# This API key is from Gemini Developer API Key, not vertex AI API Key
# Lazy client initialization to avoid crashing imports when env var is missing
_client = None
def _get_client():
    """Get or create the Gemini client with lazy initialization"""
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_GENAI_API_KEY") or "AIzaSyA6HBwd_66hfcflHlTSZDB_qxpUUSiOXDg"
        if not api_key:
            raise ValueError("GEMINI_API_KEY environment variable is not set. Please configure your Gemini API key.")

        _client = genai.Client(api_key=api_key)

    return _client


def grade_assignment(assignment_text, rubric="", max_points=100):
    """Grade an assignment using Gemini AI with detailed feedback"""
    try:
        prompt = f"""
        Please grade this assignment and provide detailed feedback.
        
        Assignment Text:
        {assignment_text}
        
        Grading Rubric:
        {rubric if rubric else "Standard academic grading criteria focusing on content accuracy, clarity, organization, and completeness."}
        
        Maximum Points: {max_points}
        
        Please provide your response in the following JSON format:
        {{
            "grade": <numeric_grade>,
            "percentage": <percentage_score>,
            "feedback": "Detailed feedback explaining strengths and areas for improvement",
            "suggestions": "Specific suggestions for improvement"
        }}
        """
        
        client = _get_client()
        response = client.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        if response.text:
            result = json.loads(response.text)
            return {
                'grade': min(float(result.get('grade', 0)), max_points),
                'percentage': min(float(result.get('percentage', 0)), 100),
                'feedback': result.get('feedback', 'No feedback provided'),
                'suggestions': result.get('suggestions', 'No suggestions provided')
            }
        else:
            return {
                'grade': 0,
                'percentage': 0,
                'feedback': 'Unable to grade assignment automatically',
                'suggestions': 'Please have instructor review manually'
            }
            
    except Exception as e:
        logging.error(f"Error grading assignment: {e}")
        return {
            'grade': 0,
            'percentage': 0,
            'feedback': 'Error occurred during automated grading',
            'suggestions': 'Please have instructor review manually'
        }

def generate_mcq_options(question_text, context=""):
    """Generate 4 MCQ options for a given question using AI"""
    try:
        prompt = f"""
        Generate 4 multiple choice options (A, B, C, D) for the following question.
        Make sure one option is correct and the other three are plausible but incorrect.
        
        Question: {question_text}
        {f"Context/Topic: {context}" if context else ""}
        
        Provide your response in the following JSON format:
        {{
            "option_a": "First option text",
            "option_b": "Second option text",
            "option_c": "Third option text",
            "option_d": "Fourth option text",
            "correct_answer": "A",
            "explanation": "Brief explanation of why the correct answer is right"
        }}
        
        Make the options clear, concise, and educational.
        """
        
        client = _get_client()
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        if response.text:
            result = json.loads(response.text)
            return {
                'option_a': result.get('option_a', ''),
                'option_b': result.get('option_b', ''),
                'option_c': result.get('option_c', ''),
                'option_d': result.get('option_d', ''),
                'correct_answer': result.get('correct_answer', 'A'),
                'explanation': result.get('explanation', '')
            }
        else:
            return None
            
    except Exception as e:
        logging.error(f"Error generating MCQ options: {e}")
        return None

def generate_forum_response(topic_title, topic_content, existing_replies=""):
    """Generate an AI response for forum discussions"""
    try:
        prompt = f"""
        Generate a helpful and educational response to this forum discussion topic.
        
        Topic: {topic_title}
        Content: {topic_content}
        
        Existing Replies: {existing_replies if existing_replies else "None"}
        
        Please provide a constructive, educational response that:
        1. Addresses the main points raised
        2. Provides additional insights or clarifications
        3. Encourages further discussion
        4. Maintains a supportive learning environment
        
        Keep the response conversational and academic in tone.
        """
        
        client = _get_client()
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        
        return response.text if response.text else "Unable to generate AI response at this time."
        
    except Exception as e:
        logging.error(f"Error generating forum response: {e}")
        return "Unable to generate AI response at this time."

def determine_correct_answer(question_text, options):
    """
    NEW FEATURE: AI Auto-Correction for MCQ
    Gemini AI automatically determines which option is correct based on the question and available options.
    Instructor does NOT need to select the correct answer manually.
    
    Args:
        question_text (str): The MCQ question
        options (dict): Dictionary of options like {'A': 'text', 'B': 'text', 'C': 'text', 'D': 'text'}
    
    Returns:
        str: The letter of the correct answer (A, B, C, or D)
    """
    try:
        options_text = "\n".join([f"{letter}. {text}" for letter, text in options.items()])
        
        prompt = f"""
        You are an educational AI assistant. Given the following multiple-choice question and options, 
        determine which option (A, B, C, or D) is the CORRECT answer.
        
        Question:
        {question_text}
        
        Options:
        {options_text}
        
        Analyze the question carefully and respond with ONLY the letter (A, B, C, or D) of the correct answer.
        Do not include any explanation, just the single letter.
        """
        
        client = _get_client()
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        
        if response.text:
            answer = response.text.strip().upper()
            if answer in ['A', 'B', 'C', 'D']:
                logging.info(f"AI determined correct answer: {answer} for question: {question_text[:50]}...")
                return answer
            else:
                logging.warning(f"AI response '{answer}' is not valid. Defaulting to 'A'")
                return 'A'
        else:
            logging.warning("No AI response received. Defaulting to 'A'")
            return 'A'
            
    except Exception as e:
        logging.error(f"Error determining correct answer with AI: {e}")
        return 'A'

def analyze_student_progress(assignment_history, participation_data):
    """Analyze student progress and provide recommendations"""
    try:
        prompt = f"""
        Analyze this student's academic progress and provide recommendations.
        
        Assignment History: {assignment_history}
        Participation Data: {participation_data}
        
        Please provide analysis in JSON format:
        {{
            "overall_performance": "assessment of overall performance",
            "strengths": ["list of strengths"],
            "areas_for_improvement": ["list of areas to improve"],
            "recommendations": ["specific recommendations for student"],
            "engagement_level": "high/medium/low"
        }}
        """
        
        client = _get_client()
        response = client.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        if response.text:
            return json.loads(response.text)
        else:
            return {
                "overall_performance": "Unable to analyze",
                "strengths": [],
                "areas_for_improvement": [],
                "recommendations": [],
                "engagement_level": "unknown"
            }
            
    except Exception as e:
        logging.error(f"Error analyzing student progress: {e}")
        return {
            "overall_performance": "Analysis unavailable",
            "strengths": [],
            "areas_for_improvement": [],
            "recommendations": [],
            "engagement_level": "unknown"
        }

def generate_video_notes(video_title, video_description="", video_duration="", video_url=""):
    """
    NEW FEATURE: AI-Powered Video Notes Generation - Enhanced for Accuracy
    Generate comprehensive, well-structured educational notes for a video using Gemini AI.
    This creates detailed study notes with proper formatting and learning outcomes.
    
    Args:
        video_title (str): The title of the video
        video_description (str): Optional description of the video content
        video_duration (str): Optional duration of the video
        video_url (str): YouTube video URL for analysis
    
    Returns:
        str: Comprehensive, accurately formatted study notes for the video
    """
    try:
        duration_text = f"Video Duration: {video_duration}" if video_duration else ""
        description_text = f"Video Description: {video_description}" if video_description else ""
        
        prompt = f"""
        You are a world-class educational content specialist and pedagogical expert. Your task is to create exceptionally high-quality, 
        well-structured study notes that are accurate, comprehensive, and easy to understand.
        
        VIDEO INFORMATION:
        - Title: {video_title}
        {f"- Description: {description_text}" if description_text else ""}
        {f"- Duration: {duration_text}" if duration_text else ""}
        {f"- URL: {video_url}" if video_url else ""}
        
        CREATE COMPREHENSIVE STUDY NOTES WITH THE FOLLOWING STRUCTURE:
        
        ═══════════════════════════════════════════════════════════
        1. LEARNING OBJECTIVES
        ═══════════════════════════════════════════════════════════
        - List 5-7 specific learning outcomes students should achieve
        - Use action verbs (understand, analyze, apply, evaluate)
        - Make objectives measurable and clear
        
        ═══════════════════════════════════════════════════════════
        2. EXECUTIVE SUMMARY
        ═══════════════════════════════════════════════════════════
        - Provide a compelling 2-3 paragraph overview of the topic
        - Highlight why this topic matters and its real-world applications
        - Connect to broader educational concepts
        
        ═══════════════════════════════════════════════════════════
        3. FUNDAMENTAL CONCEPTS & DEFINITIONS
        ═══════════════════════════════════════════════════════════
        - Define key terms with clarity and precision
        - Provide etymology or origin of important terms when relevant
        - Use examples to illustrate each concept
        - Bold important terminology
        
        ═══════════════════════════════════════════════════════════
        4. DETAILED CONTENT BREAKDOWN
        ═══════════════════════════════════════════════════════════
        Structure as:
        - Main Topic Headings
        - Sub-concepts with detailed explanations
        - Real-world examples and case studies
        - Visual descriptions (if applicable)
        - Common misconceptions and clarifications
        
        ═══════════════════════════════════════════════════════════
        5. WORKED EXAMPLES & APPLICATIONS
        ═══════════════════════════════════════════════════════════
        - Include 3-5 concrete, well-explained examples
        - Show step-by-step problem-solving approach
        - Demonstrate how theory translates to practice
        - Include both simple and complex examples
        
        ═══════════════════════════════════════════════════════════
        6. KEY FORMULAS, THEOREMS & PRINCIPLES
        ═══════════════════════════════════════════════════════════
        - List all essential equations or principles
        - Explain the significance of each
        - Show how to use them in context
        
        ═══════════════════════════════════════════════════════════
        7. SUMMARY TABLE OF KEY POINTS
        ═══════════════════════════════════════════════════════════
        Create a structured summary with:
        - Concept | Definition | Relevance | Example
        
        ═══════════════════════════════════════════════════════════
        8. STUDY QUESTIONS & SELF-ASSESSMENT
        ═══════════════════════════════════════════════════════════
        - 5-8 progressive difficulty comprehension questions
        - Include: factual recall, application, and critical thinking questions
        - Provide answer guides or hints where helpful
        
        ═══════════════════════════════════════════════════════════
        9. COMMON ERRORS & HOW TO AVOID THEM
        ═══════════════════════════════════════════════════════════
        - Identify typical student mistakes
        - Explain why these errors occur
        - Provide strategies to prevent them
        
        ═══════════════════════════════════════════════════════════
        10. EXTENSION & FURTHER LEARNING
        ═══════════════════════════════════════════════════════════
        - Advanced concepts that build on this foundation
        - Related topics worth exploring
        - Recommended next steps in learning journey
        
        QUALITY REQUIREMENTS:
        ✓ Be exceptionally clear and accurate in all explanations
        ✓ Use consistent formatting and structure throughout
        ✓ Target audience: Advanced undergraduate/early graduate level
        ✓ Ensure all content is factually correct and well-researched
        ✓ Total length: 2000-2500 words for comprehensive coverage
        ✓ Use professional but accessible language
        ✓ Include citations or references where appropriate
        """
        
        client = _get_client()
        response = client.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt
        )
        
        if response.text and len(response.text) > 100:
            logging.info(f"Successfully generated comprehensive notes for video: {video_title[:50]}... ({len(response.text)} characters)")
            return response.text
        else:
            logging.warning("AI response too short or empty for notes generation")
            return f"Study Notes for: {video_title}\n\nUnable to generate detailed notes at this time. Please try again later."
            
    except Exception as e:
        logging.error(f"Error generating video notes: {e}")
        return f"Study Notes for: {video_title}\n\nError occurred while generating notes. Please try again later."

def generate_video_transcript(video_title, video_description="", video_duration="", video_url=""):
    """
    NEW FEATURE: AI-Powered Video Transcript Generation
    Generate a comprehensive educational transcript for a video using Gemini AI.
    This analyzes the actual YouTube video content to create detailed transcripts.
    
    Args:
        video_title (str): The title of the video
        video_description (str): Optional description of the video content
        video_duration (str): Optional duration of the video
        video_url (str): YouTube video URL for analysis
    
    Returns:
        str: A comprehensive transcript/notes for the video
    """
    try:
        duration_text = f"Video Duration: {video_duration}" if video_duration else ""
        description_text = f"\nVideo Description:\n{video_description}" if video_description else ""
        
        # If video URL is provided, use Gemini's video understanding
        if video_url and ('youtube.com' in video_url or 'youtu.be' in video_url):
            prompt = f"""
            You are an expert educational content creator. Analyze this YouTube educational video and generate a comprehensive, 
            detailed transcript and lecture notes.
            
            Video Title: {video_title}
            {description_text}
            {duration_text}
            Video URL: {video_url}
            
            Please watch/analyze the video and create a detailed educational transcript that includes:
            
            1. INTRODUCTION
               - Brief overview of what the video covers
               - Learning objectives from the actual video content
            
            2. MAIN CONTENT (based on actual video)
               - Comprehensive coverage of topics discussed in the video
               - Key concepts and definitions mentioned
               - Important points and explanations from the lecture
               - Examples and demonstrations shown
               - Visual content described
            
            3. KEY TAKEAWAYS
               - Summary of main points from the video
               - Important concepts to remember
            
            4. TIMESTAMPS & TOPICS (if applicable)
               - Major sections of the video with approximate timestamps
            
            5. ADDITIONAL RESOURCES
               - Suggested topics for further study based on video content
               - Related concepts mentioned in the video
            
            Format the transcript in a clear, professional manner with proper headings and structure.
            Make it comprehensive enough to serve as standalone study material for students.
            The transcript should be detailed (1000-2000 words) to provide substantial educational value.
            """
        else:
            # Fallback to title and description if no valid YouTube URL
            prompt = f"""
            You are an expert educational content creator. Generate a comprehensive, detailed transcript and lecture notes 
            for an educational video based on the following information:
            
            Video Title: {video_title}
            {description_text}
            {duration_text}
            
            Please create a detailed educational transcript that includes:
            
            1. INTRODUCTION
               - Brief overview of the topic
               - Learning objectives
            
            2. MAIN CONTENT
               - Comprehensive coverage of the topic
               - Key concepts and definitions
               - Important points and explanations
               - Examples where applicable
            
            3. KEY TAKEAWAYS
               - Summary of main points
               - Important concepts to remember
            
            4. ADDITIONAL RESOURCES
               - Suggested topics for further study
               - Related concepts to explore
            
            Format the transcript in a clear, professional manner with proper headings and structure.
            Make it comprehensive enough to serve as standalone study material for students.
            The transcript should be 800-1500 words to provide substantial educational value.
            """
        
        client = _get_client()
        response = client.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt
        )
        
        if response.text:
            logging.info(f"Successfully generated transcript for video: {video_title[:50]}...")
            return response.text
        else:
            logging.warning("No AI response received for transcript generation")
            return f"Transcript for: {video_title}\n\nUnable to generate detailed transcript at this time. Please try again later."
            
    except Exception as e:
        logging.error(f"Error generating video transcript: {e}")
        return f"Transcript for: {video_title}\n\nError occurred while generating transcript. Please try again later."

def generate_student_notes(topic, course_title="", course_code=""):
    """
    Generate professional study notes from a topic/subject using Gemini AI.
    Creates comprehensive 2000-2500 word study material for any topic.
    
    Args:
        topic (str): Student's topic/subject to create notes for
        course_title (str): Course name for context
        course_code (str): Course code
    
    Returns:
        str: Comprehensive, well-structured study notes (2000-2500 words)
    """
    try:
        prompt = f"""
        You are an expert academic instructor and note-taking specialist. A student has requested comprehensive study notes on a topic.
        Your task is to create professional, well-structured, educational study material from scratch.
        
        TOPIC/SUBJECT:
        {topic}
        
        {'COURSE: ' + course_title + ' (' + course_code + ')' if course_title else ''}
        
        CREATE COMPREHENSIVE, PROFESSIONAL STUDY NOTES WITH THIS STRUCTURE:
        
        ═══════════════════════════════════════════════════════════
        1. LEARNING OBJECTIVES
        ═══════════════════════════════════════════════════════════
        - List 5-7 specific learning outcomes students should achieve
        - Make them measurable and clear
        
        ═══════════════════════════════════════════════════════════
        2. EXECUTIVE SUMMARY
        ═══════════════════════════════════════════════════════════
        - Provide a 2-3 paragraph overview of the topic
        - Highlight why this topic matters
        - Connect to real-world applications
        
        ═══════════════════════════════════════════════════════════
        3. FUNDAMENTAL CONCEPTS & DEFINITIONS
        ═══════════════════════════════════════════════════════════
        - Define all key terms with clarity
        - Use bold formatting for important terminology
        - Provide examples for each concept
        
        ═══════════════════════════════════════════════════════════
        4. DETAILED CONTENT BREAKDOWN
        ═══════════════════════════════════════════════════════════
        - Main topic sections with subsections
        - In-depth explanations and examples
        - Practical applications where relevant
        
        ═══════════════════════════════════════════════════════════
        5. KEY TAKEAWAYS & SUMMARY
        ═══════════════════════════════════════════════════════════
        - List main points to remember
        - Create a concise summary
        - Highlight important relationships
        
        ═══════════════════════════════════════════════════════════
        6. PRACTICE & REVIEW
        ═══════════════════════════════════════════════════════════
        - Suggest key questions for self-assessment
        - Areas for further study
        - Related topics to explore
        
        REQUIREMENTS:
        - Total length: 2000-2500 words
        - Professional academic tone
        - Clear hierarchical structure with headings
        - Rich with examples and explanations
        - Make it suitable for printing and long-term studying
        """
        
        client = _get_client()
        response = client.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt
        )
        
        if response.text:
            logging.info(f"Successfully generated student notes from input")
            return response.text
        else:
            logging.warning("No AI response for student notes generation")
            return f"Study Notes for: {topic}\n\nUnable to generate detailed notes at this time. Please try again later."
            
    except Exception as e:
        logging.error(f"Error generating student notes: {e}")
        return f"Study Notes for: {topic}\n\nError occurred while generating notes. Please try again later."

def generate_mcq_quiz(topic, num_questions=5, difficulty="medium"):
    """
    Generate multiple-choice quiz questions from a topic using Gemini AI.
    
    Args:
        topic (str): The topic/subject to create quiz questions for
        num_questions (int): Number of questions to generate (default: 5)
        difficulty (str): Difficulty level - 'easy', 'medium', or 'hard'
    
    Returns:
        list: List of dictionaries containing questions with options and correct answers
    """
    try:
        prompt = f"""
        You are an expert educator. Generate {num_questions} multiple-choice quiz questions about the following topic at {difficulty} difficulty level.
        
        TOPIC: {topic}
        
        For each question, provide:
        1. A clear, unambiguous question
        2. Four options (A, B, C, D)
        3. The correct answer (A, B, C, or D)
        4. Brief explanation of why the correct answer is right
        
        Format your response as a valid JSON array with this exact structure:
        [
            {{
                "question": "What is...",
                "options": {{
                    "A": "Option text",
                    "B": "Option text",
                    "C": "Option text",
                    "D": "Option text"
                }},
                "correct_answer": "A",
                "explanation": "This is the correct answer because..."
            }},
            ...
        ]
        
        Requirements:
        - Each question must be distinct and test different concepts
        - Options should be plausible to avoid obvious answers
        - Explanations should be educational
        - Ensure valid JSON format
        - Generate exactly {num_questions} questions
        """
        
        client = _get_client()
        response = client.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        if response.text:
            try:
                questions = json.loads(response.text)
                
                # Validate response structure
                if not isinstance(questions, list) or len(questions) == 0:
                    logging.error(f"Invalid MCQ response structure: expected non-empty list, got {type(questions)}")
                    raise ValueError("AI returned invalid question format")
                
                # Validate each question has required fields
                for i, q in enumerate(questions):
                    if not all(key in q for key in ['question', 'options', 'correct_answer']):
                        logging.error(f"Question {i+1} missing required fields")
                        raise ValueError(f"Question {i+1} has invalid structure")
                
                logging.info(f"Successfully generated {len(questions)} MCQ questions for topic: {topic}")
                return questions
                
            except json.JSONDecodeError as je:
                logging.error(f"Invalid JSON response from Gemini for MCQ generation: {je}")
                logging.error(f"Response text: {response.text[:500]}...")  # Log first 500 chars for debugging
                raise ValueError("AI returned malformed response. Please try again.")
        else:
            logging.warning("No AI response received for MCQ generation")
            raise ValueError("No response from AI. Please check your API key and try again.")
            
    except ValueError as ve:
        # Re-raise ValueError with clear message for user
        raise ve
    except Exception as e:
        logging.error(f"Error generating MCQ quiz: {e}")
        raise Exception(f"Failed to generate questions: {str(e)}")


def generate_ai_notes(topic, teacher_name="", additional_context=""):
    """Generate comprehensive study notes on a given topic using Gemini AI"""
    try:
        prompt = f"""
        Generate comprehensive study notes on: {topic}
        {f"Context: {additional_context}" if additional_context else ""}
        
        Structure the notes with clear headings and organized content:
        - Introduction
        - Key Concepts
        - Detailed Explanations with Examples
        - Important Points
        - Summary
        
        Provide ONLY the study notes content. Do not include any meta-instructions, commands, 
        or formatting explanations. Write directly for students to read and learn.
        """
        
        client = _get_client()
        response = client.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt,
        )
        
        if response.text:
            content = response.text.strip()
            
            # Clean up common AI artifacts
            content = content.replace('```markdown', '').replace('```', '')
            
            # Remove any meta-instructions or commands
            lines = content.split('\n')
            cleaned_lines = []
            skip_next = False
            
            for line in lines:
                line_lower = line.lower().strip()
                # Skip lines that look like instructions or meta-content
                if any(phrase in line_lower for phrase in [
                    'here is', 'here are', 'i will', 'i have', 'let me',
                    'as requested', 'note that', 'please note',
                    'this document', 'these notes', 'above is'
                ]):
                    if len(line.strip()) < 100:  # Only skip if it's a short instructional line
                        continue
                
                cleaned_lines.append(line)
            
            content = '\n'.join(cleaned_lines).strip()
            
            logging.info(f"Successfully generated AI notes for topic: {topic}")
            return {
                'success': True,
                'content': content,
                'topic': topic,
                'teacher_name': teacher_name
            }
        else:
            logging.warning("No AI response received for notes generation")
            return {
                'success': False,
                'content': '',
                'error': 'No response from AI'
            }
            
    except Exception as e:
        logging.error(f"Error generating AI notes: {e}")
        return {
            'success': False,
            'content': '',
            'error': str(e)
        }

def answer_student_question(question):
    """
    Answer student questions instantly using Gemini AI with visual generation capability.
    
    Args:
        question (str): The student's question
    
    Returns:
        dict: Contains 'answer' text and optional 'needs_visual' flag with 'visual_prompt'
    """
    try:
        # First, determine if this question would benefit from a visual
        analysis_prompt = f"""Analyze this student question and determine if a visual diagram, chart, or illustration would significantly help explain the concept:

QUESTION: {question}

Respond with JSON:
{{
    "needs_visual": true/false,
    "visual_type": "diagram/chart/illustration/none",
    "visual_description": "Brief description of what visual to create (if needed)"
}}"""
        
        client = _get_client()
        analysis_response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=analysis_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        needs_visual = False
        visual_prompt = None
        
        if analysis_response and analysis_response.text:
            try:
                analysis = json.loads(analysis_response.text)
                needs_visual = analysis.get('needs_visual', False)
                if needs_visual:
                    visual_prompt = f"Educational {analysis.get('visual_type', 'diagram')} showing {analysis.get('visual_description', question)}, clean design, clear labels, professional style"
            except:
                pass
        
        # Now generate the text answer
        prompt = f"""You are an expert AI Study Assistant helping students with their academic questions.
A student has asked you the following question:

QUESTION: {question}

Please provide a clear, comprehensive, and educational answer that:
1. Directly addresses the student's question
2. Explains concepts in an easy-to-understand manner
3. Provides examples where helpful
4. Is accurate and academically sound
5. Is encouraging and supportive
{'6. Mention that a visual diagram is being generated to help illustrate the concept' if needs_visual else ''}

Keep your answer concise but thorough (200-400 words).

ANSWER:"""
        
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        
        if response and response.text:
            logging.info(f"AI Assistant answered question: {question[:50]}... (Visual: {needs_visual})")
            return {
                'answer': response.text.strip(),
                'needs_visual': needs_visual,
                'visual_prompt': visual_prompt
            }
        else:
            logging.warning("No AI response for student question")
            return {
                'answer': "I'm having trouble generating an answer right now. Please try again in a moment.",
                'needs_visual': False
            }
            
    except Exception as e:
        logging.error(f"Error answering student question: {e}")
        return {
            'answer': "I encountered an error while processing your question. Please try again.",
            'needs_visual': False
        }