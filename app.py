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
        # Install chromium browser only (deps handled by packages.txt)
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False
        )
        
        if result.returncode != 0:
            # Try without --with-deps if it fails
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True,
                text=True,
                timeout=300,
                check=False
            )
        
        return result.returncode == 0
        
    except subprocess.TimeoutExpired:
        st.error("⏱️ Playwright installation timed out. Please refresh the page.")
        return False
    except Exception as e:
        st.error(f"❌ Failed to install Playwright: {str(e)}")
        return False

# Install Playwright on startup
installation_status = st.empty()
with installation_status:
    with st.spinner("🔧 Setting up browser environment (first run may take 2-3 minutes)..."):
        playwright_ready = install_playwright()

if playwright_ready:
    installation_status.success("✅ Browser environment ready!")
    time.sleep(1)
    installation_status.empty()

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
    
    # Scroll to bottom multiple times to trigger ALL lazy loading
    page.evaluate("""
        async () => {
            await new Promise((resolve) => {
                let totalHeight = 0;
                let distance = 300;
                let scrollCount = 0;
                let timer = setInterval(() => {
                    let scrollHeight = document.body.scrollHeight;
                    window.scrollBy(0, distance);
                    totalHeight += distance;
                    scrollCount++;
                    
                    // Scroll back up partway to trigger any viewport-based lazy loading
                    if (scrollCount % 5 === 0) {
                        window.scrollBy(0, -1000);
                    }
                    
                    if(totalHeight >= scrollHeight * 1.5){  // Go beyond to ensure everything loads
                        clearInterval(timer);
                        window.scrollTo(0, 0);  // Scroll back to top
                        resolve();
                    }
                }, 100);
            });
        }
    """)
    
    # Wait for lazy images to load
    page.wait_for_timeout(4000)
    
    # Scroll one more time slowly
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2000)
    
    # Extract ALL image sources including lazy-loaded ones
    img_data = page.evaluate("""
        () => {
            const images = [];
            
            // Get all img elements
            document.querySelectorAll('img').forEach(img => {
                // Check various attributes where image URLs might be
                const src = img.src || img.getAttribute('src') || 
                           img.getAttribute('data-src') || 
                           img.getAttribute('data-lazy-src') ||
                           img.getAttribute('data-original') ||
                           img.getAttribute('data-srcset');
                           
                if (src && src.startsWith('http')) {
                    images.push(src);
                }
                
                // Also check srcset
                const srcset = img.srcset || img.getAttribute('srcset');
                if (srcset) {
                    srcset.split(',').forEach(s => {
                        const url = s.trim().split(' ')[0];
                        if (url && url.startsWith('http')) {
                            images.push(url);
                        }
                    });
                }
            });
            
            return images;
        }
    """)
    
    if img_data:
        images.extend(img_data)
    
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
                            if (url.startsWith('http')) {
                                images.push(url);
                            }
                        });
                    }
                }
            });
            return images;
        }
    """)
    
    if bg_images:
        images.extend(bg_images)
    
    # Remove duplicates and data URLs
    unique_images = list(set([img for img in images if img.startswith('http')]))
    
    return unique_images


def get_all_article_links(page, base_url, max_pages):
    """Get all article links handling pagination"""
    article_links = set()
    
    progress_placeholder = st.empty()
    base_domain = urlparse(base_url).netloc
    
    try:
        # Navigate to homepage
        page.goto(base_url, wait_until='networkidle', timeout=30000)
        progress_placeholder.info(f"🔍 Starting discovery from homepage...")
        
        # Click "Load More" button multiple times to load all articles
        load_more_clicks = 0
        max_load_more_clicks = 20  # Prevent infinite loop
        
        while load_more_clicks < max_load_more_clicks:
            try:
                # Scroll to bottom to trigger lazy loading
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(2)
                
                # Look for "Load More" button with various selectors
                load_more_button = page.query_selector('button:has-text("Load more"), a:has-text("Load more"), .load-more, button.loadmore, a.loadmore, [aria-label*="Load more"]')
                
                if load_more_button and load_more_button.is_visible():
                    progress_placeholder.info(f"🔄 Loading more articles... (clicked {load_more_clicks + 1} times)")
                    load_more_button.click()
                    page.wait_for_timeout(3000)  # Wait for content to load
                    load_more_clicks += 1
                else:
                    break  # No more "Load More" button
            except Exception as e:
                break  # Can't find or click button anymore
        
        progress_placeholder.info(f"✅ Loaded all articles (clicked Load More {load_more_clicks} times)")
        
        # Scroll through entire page to ensure all lazy images load
        page.evaluate("""
            async () => {
                await new Promise((resolve) => {
                    let totalHeight = 0;
                    let distance = 200;
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
        page.wait_for_timeout(3000)
        
        # Now extract ALL article links from the page
        article_links_found = page.evaluate("""
            () => {
                const links = new Set();
                
                // Find all article links - look for common WordPress article selectors
                const selectors = [
                    'article a[href]',
                    '.article a[href]',
                    '.post a[href]',
                    '.entry a[href]',
                    'h2 a[href]',
                    'h3 a[href]',
                    '.article-title a[href]',
                    '.entry-title a[href]',
                    '.post-title a[href]',
                    '[class*="article"] a[href]',
                    '[class*="post"] a[href]'
                ];
                
                selectors.forEach(selector => {
                    document.querySelectorAll(selector).forEach(a => {
                        const href = a.href;
                        // Only get links that look like content pages
                        if (href && 
                            !href.includes('#') && 
                            !href.includes('javascript:') &&
                            !href.includes('/category/') &&
                            !href.includes('/tag/') &&
                            !href.includes('/author/') &&
                            href.length > 10) {
                            links.add(href);
                        }
                    });
                });
                
                return Array.from(links);
            }
        """)
        
        # Filter for internal links only
        for link in article_links_found:
            if is_internal_url(link, base_domain):
                article_links.add(link)
        
        # Also add the homepage itself
        article_links.add(base_url)
        
        progress_placeholder.success(f"✅ Discovery complete! Found {len(article_links)} pages to check")
        
        if len(article_links) <= 1:
            progress_placeholder.warning(f"⚠️ Only found homepage. This might indicate an issue with article detection. Will check homepage thoroughly.")
        
        return list(article_links)[:max_pages]
        
    except Exception as e:
        progress_placeholder.error(f"❌ Error during discovery: {str(e)}")
        return [base_url]  # At minimum, return homepage


def check_image_status(page, image_url):
    """Check HTTP status of an image with better error handling"""
    try:
        # Validate URL first
        if not image_url or not image_url.startswith('http'):
            return 0  # Invalid URL
        
        # Check with shorter timeout to speed things up
        response = page.request.get(image_url, timeout=15000)
        status = response.status
        
        # Additional check: if it's 200 but content-type is not an image, mark as suspicious
        if status == 200:
            content_type = response.headers.get('content-type', '').lower()
            if content_type and 'image' not in content_type and 'octet-stream' not in content_type:
                # It's returning HTML or something else, not an image
                return 404  # Treat as not found
        
        return status
        
    except Exception as e:
        error_str = str(e).lower()
        
        # Categorize errors
        if 'timeout' in error_str or 'timed out' in error_str:
            return 0  # Timeout
        elif '404' in error_str:
            return 404
        elif '403' in error_str:
            return 403
        elif '500' in error_str or '502' in error_str or '503' in error_str:
            return 500
        else:
            return 0  # Generic connection error


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
                
                # Extract page title for better progress display
                page_title = page_url.split('/')[-2] if page_url.endswith('/') else page_url.split('/')[-1]
                status_text.info(f"📄 Checking page {idx + 1}/{total_pages}: {page_title}")
                
                try:
                    page.goto(page_url, wait_until='networkidle', timeout=30000)
                    images = extract_images_from_page(page)
                    
                    status_text.info(f"🖼️ Found {len(images)} images on this page, checking status...")
                    
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
if st.button("🚀 Start Image Health Check", type="primary", disabled=not playwright_ready):
    if not playwright_ready:
        st.error("❌ Browser environment not ready. Please refresh the page.")
        st.stop()
    
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
            
            # Show broken images separately for easy reference
            if broken_images > 0:
                st.markdown("---")
                st.subheader("❌ Broken Images Details")
                broken_df = df[df['Status Code'] != 200]
                
                for idx, row in broken_df.iterrows():
                    with st.expander(f"❌ {row['Status']} - {row['Image URL'][:80]}..."):
                        st.write("**Page:**", row['Page URL'])
                        st.write("**Image URL:**", row['Image URL'])
                        st.write("**Status Code:**", row['Status Code'])
                        st.write("**Status:**", row['Status'])
                        st.code(row['Image URL'], language=None)
            
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
