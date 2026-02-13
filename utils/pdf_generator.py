"""
PDF Generator with Beautiful Design for LearnNest LMS
Generates professional, stylish PDF documents with LearnNest branding
Supports Unicode languages: English, Arabic, Urdu, Sindhi, and more
"""

from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY, TA_RIGHT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from datetime import datetime
import os

# Register Unicode fonts for multi-language support
try:
    # Get the absolute path to fonts directory
    FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'fonts')
    
    # Register Noto Sans (for Latin, numbers, symbols)
    noto_sans_path = os.path.join(FONTS_DIR, 'NotoSans.ttf')
    if os.path.exists(noto_sans_path):
        pdfmetrics.registerFont(TTFont('NotoSans', noto_sans_path))
    
    # Register Noto Sans Arabic (for Arabic, Urdu, Sindhi)
    noto_arabic_path = os.path.join(FONTS_DIR, 'NotoSansArabic.ttf')
    if os.path.exists(noto_arabic_path):
        pdfmetrics.registerFont(TTFont('NotoSansArabic', noto_arabic_path))
    
    UNICODE_FONTS_AVAILABLE = True
except Exception as e:
    print(f"Warning: Could not register Unicode fonts: {e}")
    UNICODE_FONTS_AVAILABLE = False

def get_font_for_text(text):
    """
    Detect if text contains Arabic/Urdu/Sindhi characters and return appropriate font
    """
    if not UNICODE_FONTS_AVAILABLE:
        return 'Helvetica'
    
    # Check for Arabic/Urdu/Sindhi characters (Unicode range)
    for char in text:
        if '\u0600' <= char <= '\u06FF' or '\u0750' <= char <= '\u077F':
            return 'NotoSansArabic'
    
    # Default to NotoSans for better Unicode support (including math symbols)
    return 'NotoSans'


class BeautifulWatermarkedCanvas(canvas.Canvas):
    """Custom canvas with beautiful LearnNest branding and watermark"""
    
    def __init__(self, *args, add_watermark=True, custom_watermark=None, **kwargs):
        canvas.Canvas.__init__(self, *args, **kwargs)
        self.pages = []
        self.add_watermark = add_watermark
        self.custom_watermark = custom_watermark
        
    def showPage(self):
        self.pages.append(dict(self.__dict__))
        self._startPage()
        
    def save(self):
        page_count = len(self.pages)
        for page_num, page in enumerate(self.pages, start=1):
            self.__dict__.update(page)
            self.draw_page_design(page_num, page_count)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)
        
    def draw_page_design(self, page_num, total_pages):
        """Draw beautiful page design with watermark and branding - Enhanced with modern design"""
        page_width, page_height = letter
        
        # Save the state
        self.saveState()
        
        # === ENHANCED WATERMARK (Diagonal) ===
        if self.add_watermark:
            watermark_font = 'NotoSans' if UNICODE_FONTS_AVAILABLE else 'Helvetica-Bold'
            try:
                self.setFont(watermark_font, 80)
            except:
                self.setFont('Helvetica-Bold', 80)
            
            # Blue watermark with transparency
            self.setFillColor(colors.HexColor("#0A84FF"), alpha=0.12)
            self.translate(page_width / 2, page_height / 2)
            self.rotate(48)
            watermark_text = "LearnNest"
            self.drawCentredString(0, 0, watermark_text)
            self.restoreState()
            self.saveState()
        
        # === CUSTOM WATERMARK (Diagonal) - Student's Personal Watermark ===
        if self.custom_watermark:
            watermark_font = 'NotoSans' if UNICODE_FONTS_AVAILABLE else 'Helvetica-Bold'
            try:
                self.setFont(watermark_font, 60)
            except:
                self.setFont('Helvetica-Bold', 60)
            
            # Student's custom watermark in white with transparency
            self.setFillColor(colors.white, alpha=0.08)
            self.translate(page_width / 2, page_height / 2 - 100)
            self.rotate(48)
            self.drawCentredString(0, 0, self.custom_watermark)
            self.restoreState()
            self.saveState()
        
        # === PREMIUM HEADER DESIGN ===
        # Gradient-effect header (dark to light purple)
        self.setFillColor(colors.HexColor("#1a0033"))
        self.rect(0, page_height - 0.18 * inch, page_width, 0.18 * inch, fill=1, stroke=0)
        
        # Accent line
        self.setFillColor(colors.HexColor("#00d4ff"))
        self.rect(0, page_height - 0.2 * inch, page_width, 0.02 * inch, fill=1, stroke=0)
        
        # LearnNest branding with modern font
        brand_font = 'NotoSans' if UNICODE_FONTS_AVAILABLE else 'Helvetica-Bold'
        try:
            self.setFont(brand_font, 16)
        except:
            self.setFont('Helvetica-Bold', 16)
        self.setFillColor(colors.white)
        self.drawString(0.75 * inch, page_height - 0.14 * inch, "LearnNest")
        
        # Tagline with sky blue accent
        try:
            self.setFont('NotoSans' if UNICODE_FONTS_AVAILABLE else 'Helvetica', 8)
        except:
            self.setFont('Helvetica', 8)
        self.setFillColor(colors.HexColor("#00d4ff"))
        self.drawString(0.75 * inch, page_height - 0.32 * inch, "Empowering Learning Through AI")
        
        # === PREMIUM FOOTER DESIGN ===
        # Footer gradient bar
        self.setFillColor(colors.HexColor("#1a0033"))
        self.rect(0, 0, page_width, 0.55 * inch, fill=1, stroke=0)
        
        # Sky blue accent line
        self.setFillColor(colors.HexColor("#00d4ff"))
        self.rect(0, 0.55 * inch, page_width, 0.02 * inch, fill=1, stroke=0)
        
        # Page number with modern styling
        try:
            self.setFont('NotoSans' if UNICODE_FONTS_AVAILABLE else 'Helvetica', 10)
        except:
            self.setFont('Helvetica', 10)
        self.setFillColor(colors.white)
        footer_text = f"Page {page_num} of {total_pages}"
        self.drawCentredString(page_width / 2, 0.35 * inch, footer_text)
        
        # Copyright text with sky blue
        try:
            self.setFont('NotoSans' if UNICODE_FONTS_AVAILABLE else 'Helvetica', 7)
        except:
            self.setFont('Helvetica', 7)
        self.setFillColor(colors.HexColor("#00d4ff"))
        self.drawCentredString(page_width / 2, 0.2 * inch, "© LearnNest LMS - AI Generated Content")


def generate_transcript_pdf(transcript_text, video_title, course_name, student_name, output_path, add_watermark=True, custom_watermark=None):
    """
    Generate a stunning, professional PDF transcript with LearnNest branding and modern design
    
    Args:
        transcript_text (str): The AI-generated transcript content
        video_title (str): Title of the video
        course_name (str): Name of the course
        student_name (str): Name of the student downloading the transcript
        output_path (str): Full path where the PDF should be saved
        add_watermark (bool): Whether to add LearnNest watermark (default: True)
    
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        # Create the PDF document with custom canvas and enhanced margins
        doc = SimpleDocTemplate(
            output_path,
            pagesize=letter,
            rightMargin=0.8*inch,
            leftMargin=0.8*inch,
            topMargin=1.0*inch,
            bottomMargin=1.0*inch
        )
        
        # Container for the 'Flowable' objects
        story = []
        
        # Define custom styles
        styles = getSampleStyleSheet()
        
        # Detect language for font selection
        content_font = get_font_for_text(transcript_text + video_title)
        
        # === STUNNING TITLE STYLE ===
        title_style = ParagraphStyle(
            'BeautifulTitle',
            parent=styles['Heading1'],
            fontSize=28,
            textColor=colors.HexColor("#1a0033"),
            spaceAfter=12,
            spaceBefore=6,
            alignment=TA_CENTER,
            fontName=content_font,
            leading=32,
            borderPadding=12,
            borderColor=colors.HexColor("#00d4ff"),
            borderWidth=0.5
        )
        
        # === MODERN SUBTITLE STYLE ===
        subtitle_style = ParagraphStyle(
            'SubtitleStyle',
            parent=styles['Normal'],
            fontSize=13,
            textColor=colors.HexColor("#555555"),
            spaceAfter=24,
            alignment=TA_CENTER,
            fontName=content_font,
            leading=16
        )
        
        # === INFO BOX STYLE ===
        info_label_style = ParagraphStyle(
            'InfoLabel',
            parent=styles['Normal'],
            fontSize=10,
            textColor=colors.HexColor("#00d4ff"),
            fontName=content_font,
            spaceAfter=2
        )
        
        info_value_style = ParagraphStyle(
            'InfoValue',
            parent=styles['Normal'],
            fontSize=11,
            textColor=colors.HexColor("#1a0033"),
            fontName=content_font,
            spaceAfter=8
        )
        
        # === VVIP PROFESSIONAL HEADING STYLE (CENTERED) ===
        heading_style = ParagraphStyle(
            'VVIPHeading',
            parent=styles['Heading2'],
            fontSize=16,
            textColor=colors.HexColor("#FFFFFF"),
            spaceAfter=16,
            spaceBefore=20,
            alignment=TA_CENTER,
            fontName=content_font,
            borderPadding=12,
            borderColor=colors.HexColor("#0A84FF"),
            borderWidth=2,
            backColor=colors.HexColor("#1a0033"),
            leading=20,
            borderRadius=8
        )
        
        # === PREMIUM BODY STYLE ===
        body_style = ParagraphStyle(
            'BeautifulBody',
            parent=styles['BodyText'],
            fontSize=10.5,
            alignment=TA_JUSTIFY,
            spaceAfter=12,
            leading=20,
            textColor=colors.HexColor("#1a1a1a"),
            fontName=content_font,
            borderPadding=6
        )
        
        # === DOCUMENT HEADER ===
        story.append(Spacer(1, 0.2*inch))
        
        # Main Title with modern styling
        title = Paragraph(f'<b>{video_title}</b>', title_style)
        story.append(title)
        
        # Document Type Subtitle
        subtitle_text = 'Study Notes & Transcript'
        if 'Study Notes' in video_title or 'notes' in video_title.lower():
            subtitle_text = 'AI-Generated Study Notes'
        elif 'transcript' in video_title.lower():
            subtitle_text = 'AI-Generated Transcript'
        
        subtitle = Paragraph(subtitle_text, subtitle_style)
        story.append(subtitle)
        
        story.append(Spacer(1, 0.25*inch))
        
        # === INFORMATION BOX ===
        # Create a beautiful info table
        info_data = [
            [Paragraph('<b>Student Name:</b>', info_label_style), Paragraph(student_name, info_value_style)],
            [Paragraph('<b>Course:</b>', info_label_style), Paragraph(course_name, info_value_style)],
            [Paragraph('<b>Generated On:</b>', info_label_style), 
             Paragraph(datetime.now().strftime("%B %d, %Y at %I:%M %p"), info_value_style)]
        ]
        
        info_table = Table(info_data, colWidths=[1.5*inch, 4.5*inch])
        info_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor("#e6f7ff")),
            ('BACKGROUND', (1, 0), (-1, -1), colors.HexColor("#f0f8ff")),
            ('BOX', (0, 0), (-1, -1), 1.5, colors.HexColor("#00d4ff")),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
            ('TOPPADDING', (0, 0), (-1, -1), 12),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, -1), content_font),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('ROWBACKGROUNDS', (0, 0), (-1, -1), [colors.white, colors.HexColor("#f0f8ff")]),
        ]))
        
        story.append(info_table)
        story.append(Spacer(1, 0.4*inch))
        
        # === CONTENT DIVIDER ===
        story.append(Spacer(1, 0.15*inch))
        
        # === TRANSCRIPT CONTENT HEADER ===
        content_header = Paragraph(
            '<b>📚  CONTENT  📚</b>',
            ParagraphStyle(
                'ContentHeader',
                parent=styles['Normal'],
                fontSize=13,
                textColor=colors.HexColor("#FFFFFF"),
                alignment=TA_CENTER,
                spaceAfter=20,
                fontName=content_font,
                borderColor=colors.HexColor("#00d4ff"),
                borderWidth=1.5,
                backColor=colors.HexColor("#1a0033"),
                borderPadding=8,
                leading=16
            )
        )
        story.append(content_header)
        story.append(Spacer(1, 0.15*inch))
        
        # === PROCESS TRANSCRIPT TEXT ===
        sections = transcript_text.split('\n\n')
        
        for section in sections:
            section = section.strip()
            if not section:
                continue
            
            lines = section.split('\n')
            first_line = lines[0].strip()
            
            # Check if this is a header
            if (first_line.isupper() and len(first_line) < 60) or \
               (first_line and first_line[0].isdigit() and '.' in first_line[:5]):
                # This is a VVIP heading - centered with elegant decorations
                heading_text = f'✦  {first_line}  ✦'
                heading = Paragraph(heading_text, heading_style)
                story.append(heading)
                
                # Add the rest as body text
                if len(lines) > 1:
                    body_text = '\n'.join(lines[1:])
                    para = Paragraph(body_text.replace('\n', '<br/>'), body_style)
                    story.append(para)
            else:
                # Regular paragraph
                para = Paragraph(section.replace('\n', '<br/>'), body_style)
                story.append(para)
            
            story.append(Spacer(1, 0.1*inch))
        
        # === FOOTER NOTE ===
        story.append(Spacer(1, 0.4*inch))
        
        # === PREMIUM FOOTER NOTE ===
        story.append(Spacer(1, 0.4*inch))
        
        footer_note_style = ParagraphStyle(
            'FooterNote',
            parent=styles['Normal'],
            fontSize=9,
            textColor=colors.HexColor("#1a1a1a"),
            alignment=TA_CENTER,
            fontName=content_font,
            leading=14,
            borderWidth=2,
            borderColor=colors.HexColor("#00d4ff"),
            borderPadding=12,
            backColor=colors.HexColor("#e6f7ff")
        )
        
        footer_note = Paragraph(
            '<b>✓ Quality Assurance:</b> This document was generated using advanced Gemini AI technology. '
            'For best results, cross-reference key concepts with original video content. '
            '<br/><b>🎓 Use This For:</b> Study reference, exam preparation, knowledge review, and learning reinforcement.'
            '<br/><i>LearnNest - Empowering Learning Through AI Technology</i>',
            footer_note_style
        )
        story.append(footer_note)
        
        # Build PDF with beautiful watermarked canvas
        def make_canvas(*args, **kwargs):
            return BeautifulWatermarkedCanvas(*args, add_watermark=add_watermark, custom_watermark=custom_watermark, **kwargs)
        
        doc.build(story, canvasmaker=make_canvas)
        
        return True
        
    except Exception as e:
        print(f"Error generating beautiful PDF: {e}")
        return False


def generate_notes_pdf(notes_content, topic, teacher_name, output_path, add_watermark=True, custom_watermark=None):
    """
    Generate a stunning, professional PDF for AI-generated notes with LearnNest branding
    
    Args:
        notes_content (str): The AI-generated notes content (can be markdown formatted)
        topic (str): The topic/subject of the notes
        teacher_name (str): Name of the instructor/teacher
        output_path (str): Full path where the PDF should be saved
        add_watermark (bool): Whether to add LearnNest watermark (default: True)
        custom_watermark (str): Custom watermark text from student (optional)
    
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        # Create the PDF document with custom canvas
        doc = SimpleDocTemplate(
            output_path,
            pagesize=letter,
            rightMargin=0.8*inch,
            leftMargin=0.8*inch,
            topMargin=1.0*inch,
            bottomMargin=1.0*inch
        )
        
        # Container for the 'Flowable' objects
        story = []
        
        # Define custom styles
        styles = getSampleStyleSheet()
        
        # Detect language for font selection
        content_font = get_font_for_text(notes_content + topic)
        
        # === TITLE STYLE ===
        title_style = ParagraphStyle(
            'NotesTitle',
            parent=styles['Heading1'],
            fontSize=28,
            textColor=colors.HexColor("#1a0033"),
            spaceAfter=12,
            spaceBefore=6,
            alignment=TA_CENTER,
            fontName=content_font,
            leading=32,
            borderPadding=12,
            borderColor=colors.HexColor("#00d4ff"),
            borderWidth=0.5
        )
        
        # === SUBTITLE STYLE ===
        subtitle_style = ParagraphStyle(
            'NotesSubtitle',
            parent=styles['Normal'],
            fontSize=13,
            textColor=colors.HexColor("#555555"),
            spaceAfter=20,
            alignment=TA_CENTER,
            fontName=content_font,
            leading=16
        )
        
        # === TEACHER NAME STYLE ===
        teacher_style = ParagraphStyle(
            'TeacherName',
            parent=styles['Normal'],
            fontSize=14,
            textColor=colors.HexColor("#00d4ff"),
            spaceAfter=24,
            alignment=TA_CENTER,
            fontName=content_font,
            leading=16
        )
        
        # === VVIP HEADING STYLE (CENTERED) ===
        heading_style = ParagraphStyle(
            'VVIPNotesHeading',
            parent=styles['Heading2'],
            fontSize=16,
            textColor=colors.HexColor("#FFFFFF"),
            spaceAfter=16,
            spaceBefore=20,
            alignment=TA_CENTER,
            fontName=content_font,
            borderPadding=12,
            borderColor=colors.HexColor("#0A84FF"),
            borderWidth=2,
            backColor=colors.HexColor("#1a0033"),
            leading=20,
            borderRadius=8
        )
        
        # === BODY STYLE ===
        body_style = ParagraphStyle(
            'NotesBody',
            parent=styles['BodyText'],
            fontSize=10.5,
            alignment=TA_JUSTIFY,
            spaceAfter=12,
            leading=20,
            textColor=colors.HexColor("#1a1a1a"),
            fontName=content_font,
            borderPadding=6
        )
        
        # === DOCUMENT HEADER ===
        story.append(Spacer(1, 0.2*inch))
        
        # Main Title
        title = Paragraph(f'<b>{topic}</b>', title_style)
        story.append(title)
        
        # Subtitle
        subtitle = Paragraph('AI-Generated Study Notes', subtitle_style)
        story.append(subtitle)
        
        # Teacher Name
        if teacher_name:
            teacher_para = Paragraph(f'<b>By: {teacher_name}</b>', teacher_style)
            story.append(teacher_para)
        
        story.append(Spacer(1, 0.3*inch))
        
        # === INFO BOX ===
        info_data = [
            [Paragraph('<b>Generated On:</b>', subtitle_style), 
             Paragraph(datetime.now().strftime("%B %d, %Y at %I:%M %p"), subtitle_style)]
        ]
        
        info_table = Table(info_data, colWidths=[1.5*inch, 4.5*inch])
        info_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#e6f7ff")),
            ('BOX', (0, 0), (-1, -1), 1.5, colors.HexColor("#00d4ff")),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTNAME', (0, 0), (-1, -1), content_font),
        ]))
        
        story.append(info_table)
        story.append(Spacer(1, 0.4*inch))
        
        # === CONTENT DIVIDER ===
        content_header = Paragraph(
            '<b>📚  STUDY NOTES  📚</b>',
            ParagraphStyle(
                'ContentHeader',
                parent=styles['Normal'],
                fontSize=13,
                textColor=colors.HexColor("#FFFFFF"),
                alignment=TA_CENTER,
                spaceAfter=20,
                fontName=content_font,
                borderColor=colors.HexColor("#00d4ff"),
                borderWidth=1.5,
                backColor=colors.HexColor("#1a0033"),
                borderPadding=8,
                leading=16
            )
        )
        story.append(content_header)
        story.append(Spacer(1, 0.15*inch))
        
        # === PROCESS NOTES CONTENT ===
        # Handle markdown-style formatting
        sections = notes_content.split('\n\n')
        
        for section in sections:
            section = section.strip()
            if not section:
                continue
            
            lines = section.split('\n')
            first_line = lines[0].strip()
            
            # Check for markdown headers
            if first_line.startswith('# '):
                heading_text = f'✦  {first_line[2:]}  ✦'
                heading = Paragraph(heading_text, heading_style)
                story.append(heading)
                
                if len(lines) > 1:
                    body_text = '\n'.join(lines[1:])
                    para = Paragraph(body_text.replace('\n', '<br/>'), body_style)
                    story.append(para)
            elif first_line.startswith('## '):
                heading_text = f'✦  {first_line[3:]}  ✦'
                heading = Paragraph(heading_text, heading_style)
                story.append(heading)
                
                if len(lines) > 1:
                    body_text = '\n'.join(lines[1:])
                    para = Paragraph(body_text.replace('\n', '<br/>'), body_style)
                    story.append(para)
            elif first_line.startswith('### '):
                heading_text = f'✦  {first_line[4:]}  ✦'
                heading = Paragraph(heading_text, heading_style)
                story.append(heading)
                
                if len(lines) > 1:
                    body_text = '\n'.join(lines[1:])
                    para = Paragraph(body_text.replace('\n', '<br/>'), body_style)
                    story.append(para)
            else:
                # Process regular content with bold markers - properly handle markdown
                content = section
                # Replace **text** with <b>text</b>
                import re
                content = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', content)
                # Escape any remaining asterisks
                content = content.replace('*', '•')
                para = Paragraph(content.replace('\n', '<br/>'), body_style)
                story.append(para)
            
            story.append(Spacer(1, 0.1*inch))
        
        # === FOOTER NOTE ===
        story.append(Spacer(1, 0.4*inch))
        
        footer_note_style = ParagraphStyle(
            'FooterNote',
            parent=styles['Normal'],
            fontSize=9,
            textColor=colors.HexColor("#1a1a1a"),
            alignment=TA_CENTER,
            fontName=content_font,
            leading=14,
            borderWidth=2,
            borderColor=colors.HexColor("#00d4ff"),
            borderPadding=12,
            backColor=colors.HexColor("#e6f7ff")
        )
        
        footer_note = Paragraph(
            '<b>✓ AI-Generated Content:</b> These notes were created using advanced Gemini AI technology. '
            'Review and supplement with additional resources for comprehensive understanding. '
            '<br/><b>🎓 Use This For:</b> Study reference, exam preparation, concept review, and knowledge reinforcement.'
            '<br/><i>LearnNest - Empowering Learning Through AI Technology</i>',
            footer_note_style
        )
        story.append(footer_note)
        
        # Build PDF with beautiful watermarked canvas
        def make_canvas(*args, **kwargs):
            return BeautifulWatermarkedCanvas(*args, add_watermark=add_watermark, custom_watermark=custom_watermark, **kwargs)
        
        doc.build(story, canvasmaker=make_canvas)
        
        return True
        
    except Exception as e:
        print(f"Error generating notes PDF: {e}")
        return False
