import hashlib
import os
import re
import sys
import time
from typing import Set, Optional
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from tqdm import tqdm


def setup_driver() -> webdriver.Chrome:
    """Setup Chrome driver with appropriate options to bypass detection."""
    chrome_options = Options()

    # Common user agent
    user_agent = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'

    # Add various options to make us look more like a real browser
    chrome_options.add_argument(f'user-agent={user_agent}')
    chrome_options.add_argument('--headless=new')  # new headless mode
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-blink-features=AutomationControlled')
    chrome_options.add_argument('--disable-extensions')
    chrome_options.add_argument('--lang=fr-FR,fr')  # Set French language for this site

    # Additional preferences
    chrome_options.add_experimental_option('excludeSwitches', ['enable-automation'])
    chrome_options.add_experimental_option('useAutomationExtension', False)

    # Create the driver
    driver = webdriver.Chrome(options=chrome_options)

    # Execute CDP commands to mask automation
    driver.execute_cdp_cmd('Network.setUserAgentOverride', {
        "userAgent": user_agent
    })

    # Add missing webdriver properties to mask automation
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    return driver


def extract_images_from_current_state(driver: webdriver.Chrome) -> Set[str]:
    """Extract image URLs from the current page state."""
    img_urls = set()

    # Get page source and parse with BeautifulSoup
    soup = BeautifulSoup(driver.page_source, "html.parser")

    # Get <img> tag sources
    for img in soup.find_all("img"):
        if src := img.get("src"):
            img_urls.add(src)
        if srcset := img.get("srcset"):
            # Extract URLs from srcset attribute
            urls = re.findall(r'([^\s]+)\s*(?:\d+[wx])?,?', srcset)
            img_urls.update(urls)
        if 'data-src' in img.attrs:  # Many sites use data-src for lazy loading
            img_urls.add(img['data-src'])
        # Check for other common lazy loading attributes
        for attr in ['data-original', 'data-lazy-src', 'data-url', 'data-lazysrc']:
            if attr in img.attrs:
                img_urls.add(img[attr])

    # Get background images from style attributes and CSS
    elements_with_style = driver.find_elements(By.CSS_SELECTOR, "[style*='background-image']")
    for element in elements_with_style:
        style = element.get_attribute("style")
        if urls := re.findall(r'url\(["\']?([^"\')]+)', style):
            img_urls.update(urls)

    return img_urls


def get_image_urls_from_page(driver: webdriver.Chrome, url: str, wait_time: int = 10) -> Set[str]:
    """Extract image URLs from a webpage after JavaScript rendering."""
    # Clear cookies and cache
    driver.execute_cdp_cmd('Network.clearBrowserCookies', {})
    driver.execute_cdp_cmd('Network.clearBrowserCache', {})

    print("Loading page...")
    driver.get(url)

    # Wait for the page to load and accept cookies if present
    try:
        # Wait for body
        WebDriverWait(driver, wait_time).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        # Try to find and click cookie accept button (common in EU sites)
        try:
            cookie_buttons = driver.find_elements(By.XPATH, 
                '//*[contains(text(), "Accepter") or contains(text(), "Accept") or contains(@id, "accept") or contains(@class, "accept")]')
            for button in cookie_buttons:
                if button.is_displayed():
                    driver.execute_script("arguments[0].click();", button)
                    print("Accepted cookies")
                    time.sleep(1)
                    break
        except Exception as e:
            print(f"No cookie banner found or couldn't accept: {e}")

        # Wait for images to load
        WebDriverWait(driver, wait_time).until(
            EC.presence_of_element_located((By.TAG_NAME, "img"))
        )

        # Additional wait for dynamic content
        time.sleep(2)
        
    except Exception as e:
        print(f"Warning: Page load wait timed out: {e}")

    print("Scrolling and extracting images...")
    img_urls = set()
    last_height = driver.execute_script("return document.body.scrollHeight")
    scroll_pause_time = 1
    scroll_attempts = 0
    max_scroll_attempts = 5  # Limit scrolling to prevent infinite loops

    while scroll_attempts < max_scroll_attempts:
        # Get images from current view
        new_urls = extract_images_from_current_state(driver)
        previous_count = len(img_urls)
        img_urls.update(new_urls)
        current_count = len(img_urls)

        print(f"Found {current_count - previous_count} new images (Total: {current_count})")

        # Scroll down by a portion of the viewport height
        driver.execute_script(
            "window.scrollBy(0, window.innerHeight * 0.8);"
        )

        # Wait for new content to load
        time.sleep(scroll_pause_time)

        # Calculate new scroll height and compare with last scroll height
        new_height = driver.execute_script("return document.body.scrollHeight")
        current_position = driver.execute_script("return window.pageYOffset")

        # Check if we've reached the bottom or if no new images were found
        if new_height == last_height and current_position + driver.execute_script("return window.innerHeight") >= new_height:
            # Try one more time with a longer wait
            time.sleep(scroll_pause_time * 2)
            final_urls = extract_images_from_current_state(driver)
            img_urls.update(final_urls)
            break

        last_height = new_height
        scroll_attempts += 1

    # Print some debug info
    print(f"Page title: {driver.title}")
    print(f"Current URL: {driver.current_url}")
    print(f"Completed after {scroll_attempts} scroll attempts")

    return img_urls


def is_svg_image(url: str, content_type: Optional[str] = None) -> bool:
    """Check if the URL or content type indicates an SVG image."""
    # Check URL extension
    if url.lower().endswith('.svg'):
        return True

    # Check if URL contains SVG indicators
    if 'svg' in url.lower():
        return True

    # Check content type if available
    if content_type and 'svg' in content_type.lower():
        return True

    return False


def download_image(url: str, base_url: str, save_dir: str) -> Optional[str]:
    """Download an image from URL and save it."""
    try:
        # Handle relative URLs
        if not url.startswith(('http://', 'https://', 'data:')):
            url = urljoin(base_url, url)
        
        # Skip data URLs
        if url.startswith('data:'):
            return None
        
        # Check for SVG in URL before making request
        if is_svg_image(url):
            print(f"Skipping SVG image: {url}")
            return None
        
        # Make HEAD request first to check content type
        try:
            head_response = requests.head(url, timeout=5)
            content_type = head_response.headers.get('content-type', '')
            if is_svg_image(url, content_type):
                print(f"Skipping SVG image (content-type): {url}")
                return None
        except Exception:
            # If HEAD request fails, continue with normal GET request
            pass
        
        # Generate unique filename
        file_hash = hashlib.md5(url.encode()).hexdigest()
        extension = os.path.splitext(urlparse(url).path)[1]
        if not extension:
            extension = '.jpg'  # Default extension
        filename = f"{file_hash}{extension}"
        filepath = os.path.join(save_dir, filename)
        
        # Download if not exists
        if not os.path.exists(filepath):
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            
            # Final check of content type from actual response
            content_type = response.headers.get('content-type', '')
            if is_svg_image(url, content_type):
                print(f"Skipping SVG image (final check): {url}")
                return None
            
            with open(filepath, "wb") as f:
                f.write(response.content)
            
            return filepath
        
    except Exception as e:
        print(f"Error downloading {url}: {e}")
        return None


def main():
    if len(sys.argv) != 2:
        print("Usage: python img_extract.py <url>")
        sys.exit(1)
    
    url = sys.argv[1]
    save_dir = "images"
    os.makedirs(save_dir, exist_ok=True)
    
    try:
        driver = setup_driver()
        print("Extracting image URLs...")
        img_urls = get_image_urls_from_page(driver, url)
        driver.quit()
        
        print(f"Found {len(img_urls)} unique image URLs")
        
        # Download images with progress bar
        successful_downloads = 0
        with tqdm(total=len(img_urls), desc="Downloading images") as pbar:
            for img_url in img_urls:
                if download_image(img_url, url, save_dir):
                    successful_downloads += 1
                pbar.update(1)
        
        print(f"\nSuccessfully downloaded {successful_downloads} images to {save_dir}/")
        
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()