#!/usr/bin/env python3
"""
Google Classroom Bulk Downloader v2.2
Fixed 403 errors, file locks, and added multi-account support.
"""

import os
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '0'

import sys
import pickle
import re
import time
import io
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# ─── CONFIG ───────────────────────────────────────────────────────────────────

SCOPES = [
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly',
    'https://www.googleapis.com/auth/classroom.coursework.students.readonly',
    'https://www.googleapis.com/auth/classroom.announcements.readonly',
    'https://www.googleapis.com/auth/drive.readonly',
]

DOWNLOAD_DIR = Path.home() / "ClassroomDownloads"
CREDENTIALS_FILE = Path("credentials.json")
TOKEN_FILE = Path("token.pickle")

EXPORT_FORMATS = {
    'application/vnd.google-apps.document': ('application/pdf', '.pdf'),
    'application/vnd.google-apps.spreadsheet': ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.xlsx'),
    'application/vnd.google-apps.presentation': ('application/vnd.openxmlformats-officedocument.presentationml.presentation', '.pptx'),
    'application/vnd.google-apps.drawing': ('image/png', '.png'),
    'application/vnd.google-apps.form': None,
    'application/vnd.google-apps.script': ('application/vnd.google-apps.script+json', '.json'),
    'application/vnd.google-apps.site': None,
    'application/vnd.google-apps.map': None,
    'application/vnd.google-apps.fusiontable': None,
}

# ─── UTILITIES ────────────────────────────────────────────────────────────────

def sanitize_filename(name: str, max_length: int = 80) -> str:
    name = str(name) if name else "unnamed"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = name.strip('. ')
    if len(name) > max_length:
        name = name[:max_length].rsplit(' ', 1)[0]
    return name or 'unnamed'

def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path

def safe_filename(folder: Path, name: str, ext: str) -> Path:
    base = sanitize_filename(name)
    counter = 0
    while True:
        suffix = f" ({counter})" if counter > 0 else ""
        filename = f"{base}{suffix}{ext}"
        full = folder / filename
        if not full.exists():
            return full
        counter += 1

# ─── AUTHENTICATION ───────────────────────────────────────────────────────────

def authenticate(force_new: bool = False) -> Credentials:
    creds = None
    
    if not force_new and TOKEN_FILE.exists():
        with open(TOKEN_FILE, 'rb') as token:
            creds = pickle.load(token)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                print("   ⚠️  Token refresh failed, re-authenticating...")
                creds = None
        
        if not creds:
            if not CREDENTIALS_FILE.exists():
                print(f"❌ '{CREDENTIALS_FILE}' not found.")
                sys.exit(1)
            
            print("🔐 Opening browser for Google sign-in...")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(
                port=0,
                authorization_prompt_message='Please visit this URL to authorize: {url}',
                success_message='✅ Authorization successful! You can close this tab.'
            )
        
        with open(TOKEN_FILE, 'wb') as token:
            pickle.dump(creds, token)
        print("   💾 Token saved.\n")
    
    return creds

def switch_account() -> Credentials:
    """Delete token and force re-authentication with a different account."""
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        print("🔄 Cleared saved credentials. You'll sign in with a different account.\n")
    return authenticate()

# ─── DOWNLOAD ENGINE ──────────────────────────────────────────────────────────

class DownloadEngine:
    def __init__(self, drive_service):
        self.drive = drive_service
        self.downloaded_ids = set()
        self.failed_items = []
        self.stats = {'downloaded': 0, 'exported': 0, 'skipped': 0, 'failed': 0}
    
    def download_binary(self, file_id: str, dest_path: Path, file_name: str = "") -> bool:
        try:
            request = self.drive.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request, chunksize=2*1024*1024)
            
            done = False
            while not done:
                status, done = downloader.next_chunk()
            
            # Write all at once to avoid file lock issues
            with open(dest_path, 'wb') as f:
                f.write(fh.getvalue())
            
            print(f"   ✅ {file_name[:50]:<50}")
            self.stats['downloaded'] += 1
            return True
            
        except Exception as e:
            print(f"   ❌ {file_name[:50]:<50} {str(e)[:40]}")
            self.failed_items.append((file_id, str(e)))
            self.stats['failed'] += 1
            if dest_path.exists():
                try:
                    dest_path.unlink()
                except:
                    pass
            return False
    
    def export_file(self, file_id: str, dest_path: Path, export_mime: str, file_name: str = "") -> bool:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                request = self.drive.files().export_media(fileId=file_id, mimeType=export_mime)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request, chunksize=1024*1024)
                
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                
                with open(dest_path, 'wb') as f:
                    f.write(fh.getvalue())
                
                print(f"   ✅ {file_name[:50]:<50} [Exported]")
                self.stats['exported'] += 1
                return True
                
            except HttpError as e:
                if e.resp.status == 429 and attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    print(f"   ⏳ Rate limited. Waiting {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"   ❌ Export failed: {e}")
                    self.failed_items.append((file_id, str(e)))
                    self.stats['failed'] += 1
                    return False
            except Exception as e:
                print(f"   ❌ Export error: {e}")
                self.stats['failed'] += 1
                return False
        return False
    
    def handle_file(self, file_id: str, file_name: str, mime_type: str, folder: Path) -> Optional[Path]:
        if file_id in self.downloaded_ids:
            self.stats['skipped'] += 1
            return None
        
        if mime_type in EXPORT_FORMATS:
            export_info = EXPORT_FORMATS[mime_type]
            if export_info is None:
                print(f"   ⏭️  {file_name[:50]:<50} [Unsupported]")
                self.stats['skipped'] += 1
                return None
            
            export_mime, ext = export_info
            dest = safe_filename(folder, file_name, ext)
            success = self.export_file(file_id, dest, export_mime, file_name)
            
        else:
            info = self._get_file_info(file_id)
            if info:
                actual_name = info.get('name', file_name)
                actual_ext = Path(actual_name).suffix
                if not actual_ext:
                    actual_ext = self._guess_extension(mime_type)
            else:
                actual_name = file_name
                actual_ext = self._guess_extension(mime_type)
            
            dest = safe_filename(folder, Path(actual_name).stem, actual_ext or '.bin')
            success = self.download_binary(file_id, dest, actual_name)
        
        if success:
            self.downloaded_ids.add(file_id)
            return dest
        return None
    
    def _get_file_info(self, file_id: str) -> Optional[Dict]:
        try:
            return self.drive.files().get(fileId=file_id, fields='name,mimeType,size').execute()
        except:
            return None
    
    @staticmethod
    def _guess_extension(mime_type: str) -> str:
        mapping = {
            'application/pdf': '.pdf',
            'image/jpeg': '.jpg',
            'image/png': '.png',
            'image/gif': '.gif',
            'image/webp': '.webp',
            'video/mp4': '.mp4',
            'video/quicktime': '.mov',
            'audio/mpeg': '.mp3',
            'application/zip': '.zip',
            'text/plain': '.txt',
            'text/html': '.html',
            'application/json': '.json',
            'application/msword': '.doc',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
            'application/vnd.ms-excel': '.xls',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
            'application/vnd.ms-powerpoint': '.ppt',
            'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
        }
        return mapping.get(mime_type, '')

# ─── CLASSROOM SCANNER ────────────────────────────────────────────────────────

class ClassroomScanner:
    def __init__(self, classroom_service, drive_service):
        self.classroom = classroom_service
        self.engine = DownloadEngine(drive_service)
    
    def scan_all(self, course_id: str, course_folder: Path) -> Dict:
        totals = {'materials': 0, 'coursework': 0, 'announcements': 0}
        totals['materials'] = self._scan_coursework_materials(course_id, course_folder)
        totals['coursework'] = self._scan_coursework(course_id, course_folder)
        totals['announcements'] = self._scan_announcements(course_id, course_folder)
        return totals
    
    def _scan_coursework_materials(self, course_id: str, course_folder: Path) -> int:
        print("\n   📚 Coursework Materials")
        count = 0
        page_token = None
        
        while True:
            try:
                response = self.classroom.courses().courseWorkMaterials().list(
                    courseId=course_id, pageToken=page_token, pageSize=50
                ).execute()
            except HttpError as e:
                if e.resp.status == 403:
                    print("   ⚠️  No permission to view coursework materials (skipping)")
                else:
                    print(f"   ⚠️  API Error: {e}")
                break
            
            items = response.get('courseWorkMaterial', [])
            for item in items:
                count += self._process_item(course_folder, item, "material")
            
            page_token = response.get('nextPageToken')
            if not page_token:
                break
            time.sleep(0.5)
        
        return count
    
    def _scan_coursework(self, course_id: str, course_folder: Path) -> int:
        print("\n   📝 Assignments")
        count = 0
        page_token = None
        
        while True:
            try:
                response = self.classroom.courses().courseWork().list(
                    courseId=course_id, pageToken=page_token, pageSize=50
                ).execute()
            except HttpError as e:
                if e.resp.status == 403:
                    print("   ⚠️  No permission to view assignments (skipping)")
                else:
                    print(f"   ⚠️  API Error: {e}")
                break
            
            items = response.get('courseWork', [])
            for item in items:
                count += self._process_item(course_folder, item, "assignment")
            
            page_token = response.get('nextPageToken')
            if not page_token:
                break
            time.sleep(0.5)
        
        return count
    
    def _scan_announcements(self, course_id: str, course_folder: Path) -> int:
        print("\n   📢 Announcements")
        ann_folder = ensure_dir(course_folder / "_Announcements")
        count = 0
        page_token = None
        
        while True:
            try:
                response = self.classroom.courses().announcements().list(
                    courseId=course_id, pageToken=page_token, pageSize=50
                ).execute()
            except HttpError as e:
                if e.resp.status == 403:
                    print("   ⚠️  No permission to view announcements (skipping)")
                else:
                    print(f"   ⚠️  API Error: {e}")
                break
            
            items = response.get('announcements', [])
            for item in items:
                text = item.get('text', '')[:50] or "Announcement"
                fake = {'title': text, 'materials': item.get('materials', [])}
                count += self._process_item(ann_folder, fake, "announcement")
            
            page_token = response.get('nextPageToken')
            if not page_token:
                break
            time.sleep(0.5)
        
        return count
    
    def _process_item(self, parent_folder: Path, item: Dict, source: str) -> int:
        title = item.get('title', 'Untitled')
        materials = item.get('materials', [])
        
        if not materials:
            return 0
        
        item_folder = ensure_dir(parent_folder / sanitize_filename(title))
        downloaded = 0
        
        for mat in materials:
            if 'driveFile' in mat:
                df = mat['driveFile']['driveFile']
                result = self.engine.handle_file(
                    df['id'],
                    df.get('title', 'file'),
                    df.get('mimeType', 'application/octet-stream'),
                    item_folder
                )
                if result:
                    downloaded += 1
                    
            elif 'link' in mat:
                link = mat['link']
                link_path = safe_filename(item_folder, link.get('title', 'Link'), '.url')
                with open(link_path, 'w') as f:
                    f.write(f"[InternetShortcut]\nURL={link['url']}\n")
                downloaded += 1
                
            elif 'youtubeVideo' in mat:
                yt = mat['youtubeVideo']
                yt_path = safe_filename(item_folder, yt.get('title', 'YouTube'), '.url')
                with open(yt_path, 'w') as f:
                    f.write(f"[InternetShortcut]\nURL=https://youtube.com/watch?v={yt['id']}\n")
                downloaded += 1
        
        return downloaded

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def print_banner():
    print("""
╔══════════════════════════════════════════════════════════════╗
║           🎓 Google Classroom Bulk Downloader v2.2           ║
╚══════════════════════════════════════════════════════════════╝
""")

def select_courses(courses: list) -> list:
    print(f"\n📋 Found {len(courses)} course(s):\n")
    for i, c in enumerate(courses, 1):
        status = ""
        if c.get('courseState') == 'ARCHIVED':
            status = " [ARCHIVED]"
        print(f"   {i:2}. {c['name']:<40}{status}")
    
    print("\nEnter numbers (comma-separated), 'all', or 'switch' for different account:")
    choice = input("> ").strip().lower()
    
    if choice == 'all':
        return courses
    if choice == 'switch':
        return 'SWITCH'
    
    try:
        indices = [int(x.strip()) - 1 for x in choice.split(',')]
        return [courses[i] for i in indices if 0 <= i < len(courses)]
    except (ValueError, IndexError):
        print("❌ Invalid selection.")
        return []

def main():
    print_banner()
    
    print("🔐 Authenticating...")
    creds = authenticate()
    
    classroom = build('classroom', 'v1', credentials=creds)
    drive = build('drive', 'v3', credentials=creds)
    
    while True:
        print("\n📡 Fetching courses...")
        try:
            response = classroom.courses().list(studentId='me', pageSize=50).execute()
            courses = response.get('courses', [])
        except HttpError as e:
            print(f"❌ Failed: {e}")
            sys.exit(1)
        
        if not courses:
            print("   No courses found.")
            sys.exit(0)
        
        selected = select_courses(courses)
        
        if selected == 'SWITCH':
            creds = switch_account()
            classroom = build('classroom', 'v1', credentials=creds)
            drive = build('drive', 'v3', credentials=creds)
            continue
        
        if not selected:
            print("No courses selected.")
            sys.exit(0)
        
        break
    
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    base_dir = ensure_dir(DOWNLOAD_DIR / f"Download_{timestamp}")
    print(f"\n📁 Save location: {base_dir}\n")
    
    scanner = ClassroomScanner(classroom, drive)
    
    for course in selected:
        name = course['name']
        cid = course['id']
        course_folder = ensure_dir(base_dir / sanitize_filename(name))
        
        print("━" * 60)
        print(f"🏫 {name}")
        print("━" * 60)
        
        try:
            totals = scanner.scan_all(cid, course_folder)
            total_items = sum(totals.values())
            print(f"\n   📦 Items processed: {total_items}")
        except KeyboardInterrupt:
            print("\n\n⛔ Interrupted by user.")
            break
        except Exception as e:
            print(f"   ❌ Error: {e}")
        
        time.sleep(1)
    
    stats = scanner.engine.stats
    print("\n" + "═" * 60)
    print("   📊 DOWNLOAD REPORT")
    print("═" * 60)
    print(f"   ✅ Binary downloads:  {stats['downloaded']}")
    print(f"   🔄 Google exports:    {stats['exported']}")
    print(f"   ⏭️  Skipped:           {stats['skipped']}")
    print(f"   ❌ Failed:            {stats['failed']}")
    print(f"   📁 Location:          {base_dir}")
    print("═" * 60)
    
    if scanner.engine.failed_items:
        print(f"\n⚠️  {len(scanner.engine.failed_items)} item(s) failed.")

if __name__ == '__main__':
    main()