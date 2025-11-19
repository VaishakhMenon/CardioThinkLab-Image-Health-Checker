import streamlit as st
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from urllib.parse import urljoin, urlparse
import time
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
import json
import subprocess
import sys
import os

# Install Playwright browsers on first run (for Streamlit Cloud)
@st.cache_resource
def install_playwright():
    """Install Playwright browsers if not already installed"""
    try:
        # Check if browsers are installed by attempting to get browser path
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=300  # 5 minutes max
        )
        
        # Also install system dependencies
        subprocess.run(
            [sys.executable, "-m", "playwright", "install-deps", "chromium"],
            capture_output=True,
            text=True,
            timeout=300
        )
        
        return True
    except Exception as e:
        st.error(f"Failed to install Playwright: {str(e)}")
        return False

# Install Playwright on startup
with st.spinner("🔧 Setting up browser environment (first run only)..."):
    install_playwright()

# Page config
st.set_page_config(
    page_title="CardioThinkLab Image Health Checker",
    page_icon="🏥",
    layout="wide"
)

# Title and description
st.title("🏥 CardioThinkLab Image Health Checker")
st.markdown("**Automated image health monitoring with JavaScript rendering support**")

# Sidebar configuration
with st.sidebar:
    st.header("⚙️ Configuration")
    
    base_url = st.text_input(
        "Website URL",
        value="https://cardiothinklab.com",
        help="The main URL to crawl"
    )
    
    max_pages = st.number_input(
        "Max Pages to Crawl",
        min_value=1,
        max_value=1000,
        value=100,
        help="Limit the number of pages to crawl"
    )
    
    include_external = st.checkbox(
        "Check External Images",
        value=False,
        help="Include images hosted on external domains"
    )
    
    export_to_sheets = st.checkbox(
        "Export to Google Sheets",
        value=True,
        help="Automatically export results to Google Sheets"
    )
    
    st.markdown("---")
    st.markdown("### 📊 About")
    st.markdown("This tool uses Playwright to render JavaScript and detect all images including lazy-loaded ones.")


def get_google_sheets_client():
    """Initialize Google Sheets client using service account credentials from Streamlit secrets"""
    try:
        # Get credentials from Streamlit secrets
        credentials_dict = dict(st.secrets["gcp_service_account"])
        
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        
        credentials = Credentials.from_service_account_info(
            credentials_dict,
            scopes=scopes
        )
        
        client = gspread.authorize(credentials)
        return client
    except Exception as e:
        st.error(f"Failed to connect to Google Sheets: {str(e)}")
        return None


def export_to_google_sheets(df, sheet_url):
    """Export results to Google Sheets"""
    try:
        client = get_google_sheets_client()
        if not client:
            return False
        
        # Extract sheet ID from URL
        sheet_id = sheet_url.split('/d/')[1].split('/')[0]
        
        # Open the spreadsheet
        spreadsheet = client.open_by_key(sheet_id)
        
        # Create or get worksheet
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        worksheet_name = f"Scan_{timestamp}"
        
        try:
            worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=len(df)+1, cols=len(df.columns))
        except:
            worksheet = spreadsheet.worksheet(worksheet_name)
        
        # Clear existing data
        worksheet.clear()
        
        # Write headers and data
        worksheet.update([df.columns.values.tolist()] + df.values.tolist())
        
        # Format header row
        worksheet.format('A1:E1', {
            "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.8},
            "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True}
        })
        
        return True
    except Exception as e:
        st.error(f"Error exporting to Google Sheets: {str(e)}")
        return False


def is_internal_url(url, base_domain):
    """Check if URL belongs to the same domain"""
    parsed = urlparse(url)
    return parsed.netloc == '' or base_domain in parsed.netloc


def extract_images_from_page(page):
    """Extract all images from a rendered page"""
    images = []
    
    # Wait for page to be fully loaded
    page.wait_for_load_state('networkidle', timeout=30000)
    
    # Scroll to load lazy images
    page.evaluate("""
        async () => {
            await new Promise((resolve) => {
                let totalHeight = 0;
                let distance = 100;
                let timer = setInterval(() => {
                    let scrollHeight = document.body.scrollHeight;
                    window.scrollBy(0, distance);
                    totalHeight += distance;
                    if(totalHeight >= scrollHeight){
                        clearInterval(timer);
                        resolve();
                    }
                }, 100);
            });
        }
    """)
    
    # Wait a bit more for lazy images to load
    time.sleep(2)
    
    # Extract all image sources
    img_elements = page.query_selector_all('img')
    for img in img_elements:
        src = img.get_attribute('src')
        if src:
            images.append(src)
    
    # Also check for background images in CSS
    bg_images = page.evaluate("""
        () => {
            const images = [];
            const elements = document.querySelectorAll('*');
            elements.forEach(el => {
                const style = window.getComputedStyle(el);
                const bgImage = style.backgroundImage;
                if (bgImage && bgImage !== 'none') {
                    const matches = bgImage.match(/url\\(["\']?([^"\'\\)]+)["\']?\\)/g);
                    if (matches) {
                        matches.forEach(match => {
                            const url = match.replace(/url\\(["\']?([^"\'\\)]+)["\']?\\)/, '$1');
                            images.push(url);
                        });
                    }
                }
            });
            return images;
        }
    """)
    
    if bg_images:
        images.extend(bg_images)
    
    return list(set(images))  # Remove duplicates


def get_all_article_links(page, base_url, max_pages):
    """Get all article links handling pagination"""
    article_links = set()
    visited_pages = set()
    
    progress_placeholder = st.empty()
    
    def crawl_page(url, depth=0):
        if depth > 10 or url in visited_pages or len(article_links) >= max_pages:
            return
        
        visited_pages.add(url)
        progress_placeholder.info(f"🔍 Discovering articles... Found {len(article_links)} so far (Page {len(visited_pages)})")
        
        try:
            page.goto(url, wait_until='networkidle', timeout=30000)
            
            # Scroll to load any lazy-loaded content
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)
            
            # Get all article links
            links = page.evaluate("""
                () => {
                    const links = [];
                    document.querySelectorAll('a').forEach(a => {
                        const href = a.href;
                        if (href && !href.includes('#') && !href.includes('javascript:')) {
                            links.push(href);
                        }
                    });
                    return links;
                }
            """)
            
            base_domain = urlparse(base_url).netloc
            
            for link in links:
                # Filter for internal links that look like articles
                if is_internal_url(link, base_domain):
                    # Add logic to identify article URLs (customize based on your site structure)
                    parsed = urlparse(link)
                    if any(keyword in parsed.path.lower() for keyword in ['article', 'post', '2024', '2025', 'blog']):
                        article_links.add(link)
            
            # Look for pagination - numbered pages
            pagination_links = page.evaluate("""
                () => {
                    const links = [];
                    document.querySelectorAll('a.page-numbers, .pagination a, a[rel="next"]').forEach(a => {
                        links.push(a.href);
                    });
                    return links;
                }
            """)
            
            # Follow pagination
            for pag_link in pagination_links:
                if pag_link and pag_link not in visited_pages and is_internal_url(pag_link, base_domain):
                    crawl_page(pag_link, depth + 1)
            
            # Check for "Load More" button
            try:
                load_more = page.query_selector('button.load-more, a.load-more, .loadmore')
                if load_more:
                    load_more.click()
                    time.sleep(2)
                    crawl_page(url, depth)  # Re-crawl to get new links
            except:
                pass
                
        except Exception as e:
            st.warning(f"Error crawling {url}: {str(e)}")
    
    # Start crawling from base URL
    crawl_page(base_url)
    
    progress_placeholder.success(f"✅ Discovery complete! Found {len(article_links)} article pages")
    
    return list(article_links)


def check_image_status(page, image_url):
    """Check HTTP status of an image"""
    try:
        response = page.request.get(image_url, timeout=10000)
        return response.status
    except Exception as e:
        return 0  # Connection error


def crawl_and_check_images(base_url, max_pages, include_external):
    """Main crawling function"""
    results = []
    
    with sync_playwright() as p:
        # Launch browser
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        page = context.new_page()
        
        # Progress tracking
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        try:
            # Step 1: Get all article links
            status_text.info("🔍 Step 1/2: Discovering all article pages...")
            article_links = get_all_article_links(page, base_url, max_pages)
            
            if not article_links:
                st.warning("No article links found. Adding homepage to crawl list.")
                article_links = [base_url]
            
            total_pages = min(len(article_links), max_pages)
            
            # Step 2: Check images on each page
            status_text.info("🖼️ Step 2/2: Checking images on all pages...")
            
            base_domain = urlparse(base_url).netloc
            checked_images = {}  # Cache to avoid checking same image multiple times
            
            for idx, page_url in enumerate(article_links[:max_pages]):
                progress = (idx + 1) / total_pages
                progress_bar.progress(progress)
                status_text.info(f"📄 Checking page {idx + 1}/{total_pages}: {page_url}")
                
                try:
                    page.goto(page_url, wait_until='networkidle', timeout=30000)
                    images = extract_images_from_page(page)
                    
                    for img_url in images:
                        # Convert relative URLs to absolute
                        full_img_url = urljoin(page_url, img_url)
                        
                        # Skip if we should ignore external images
                        if not include_external and not is_internal_url(full_img_url, base_domain):
                            continue
                        
                        # Check if we've already checked this image
                        if full_img_url in checked_images:
                            status_code = checked_images[full_img_url]
                        else:
                            status_code = check_image_status(page, full_img_url)
                            checked_images[full_img_url] = status_code
                        
                        # Determine status
                        if status_code == 200:
                            status = "✅ OK"
                        elif status_code == 404:
                            status = "❌ NOT FOUND"
                        elif status_code == 403:
                            status = "⚠️ FORBIDDEN"
                        elif status_code == 0:
                            status = "❌ CONNECTION ERROR"
                        else:
                            status = f"⚠️ ERROR {status_code}"
                        
                        results.append({
                            'Page URL': page_url,
                            'Image URL': full_img_url,
                            'Status Code': status_code,
                            'Status': status,
                            'Checked At': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })
                
                except Exception as e:
                    st.warning(f"Error processing {page_url}: {str(e)}")
                    continue
        
        finally:
            browser.close()
    
    return results


# Main execution
if st.button("🚀 Start Image Health Check", type="primary"):
    if not base_url:
        st.error("Please enter a website URL")
    else:
        start_time = time.time()
        
        with st.spinner("Initializing crawler..."):
            results = crawl_and_check_images(base_url, max_pages, include_external)
        
        elapsed_time = time.time() - start_time
        
        if results:
            df = pd.DataFrame(results)
            
            # Display summary metrics
            st.markdown("---")
            st.subheader("📊 Scan Results")
            
            col1, col2, col3, col4 = st.columns(4)
            
            total_images = len(df)
            ok_images = len(df[df['Status Code'] == 200])
            broken_images = len(df[df['Status Code'] != 200])
            success_rate = (ok_images / total_images * 100) if total_images > 0 else 0
            
            col1.metric("Total Images", total_images)
            col2.metric("✅ Working", ok_images)
            col3.metric("❌ Broken", broken_images)
            col4.metric("Success Rate", f"{success_rate:.1f}%")
            
            st.info(f"⏱️ Scan completed in {elapsed_time:.1f} seconds")
            
            # Filter options
            st.markdown("---")
            st.subheader("🔍 Filter Results")
            
            col1, col2 = st.columns(2)
            
            with col1:
                status_filter = st.multiselect(
                    "Filter by Status",
                    options=df['Status'].unique(),
                    default=df['Status'].unique()
                )
            
            with col2:
                show_only_broken = st.checkbox("Show Only Broken Images", value=False)
            
            # Apply filters
            filtered_df = df[df['Status'].isin(status_filter)]
            if show_only_broken:
                filtered_df = filtered_df[filtered_df['Status Code'] != 200]
            
            # Display results table
            st.dataframe(
                filtered_df,
                use_container_width=True,
                height=400
            )
            
            # Export options
            st.markdown("---")
            st.subheader("💾 Export Results")
            
            col1, col2 = st.columns(2)
            
            with col1:
                csv = df.to_csv(index=False)
                st.download_button(
                    label="📥 Download as CSV",
                    data=csv,
                    file_name=f"image_health_check_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv"
                )
            
            with col2:
                if export_to_sheets:
                    if st.button("📊 Export to Google Sheets"):
                        sheet_url = "https://docs.google.com/spreadsheets/d/1k37PVPp_qKWPhn21AnstLj8GhkGng7ucf9zxMF_nOp0/edit"
                        with st.spinner("Exporting to Google Sheets..."):
                            if export_to_google_sheets(df, sheet_url):
                                st.success("✅ Successfully exported to Google Sheets!")
                            else:
                                st.error("❌ Failed to export to Google Sheets")
        else:
            st.warning("No images found during the scan.")

# Footer
st.markdown("---")
st.markdown(
    """
    <div style='text-align: center; color: #666;'>
        <p>Built for CardioThinkLab | Powered by Playwright & Streamlit</p>
    </div>
    """,
    unsafe_allow_html=True
)
