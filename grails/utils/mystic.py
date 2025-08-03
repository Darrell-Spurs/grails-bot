from PIL import Image, ImageDraw, ImageFilter
import requests
from io import BytesIO
from collections import Counter
import numpy as np
import math

def get_dominant_color(image, resize=100):
    """
    Get the dominant non-grayscale color from an image using average method
    Filters out gray/neutral colors to capture the real vibrant color
    """
    # Resize for faster processing
    img = image.copy()
    if img.mode == 'RGBA':
        # Convert RGBA to RGB on white background
        background = Image.new('RGB', img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])  # Use alpha channel as mask
        img = background
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    
    img = img.resize((resize, resize))
    
    # Get all pixels
    pixels = list(img.getdata())
    
    # Filter out grayscale/neutral colors
    def is_colorful(pixel, threshold=15):
        """Check if a pixel has enough color variation to not be grayscale"""
        r, g, b = pixel
        # Calculate the difference between max and min RGB values
        color_range = max(r, g, b) - min(r, g, b)
        # Also check if the color is not too dark or too bright
        brightness = (r + g + b) / 3
        return color_range > threshold and 20 < brightness < 235
    
    # Filter pixels to only colorful ones
    colorful_pixels = [pixel for pixel in pixels if is_colorful(pixel)]
    
    # If no colorful pixels found, fall back to all pixels
    if not colorful_pixels:
        colorful_pixels = pixels
    
    # Calculate the average color of colorful pixels
    pixels_array = np.array(colorful_pixels)
    avg_color = tuple(pixels_array.mean(axis=0).astype(int))
    return avg_color
    
def make_color_vibrant(color, saturation_boost=1.2, brightness_boost=1.5):
    """
    Make a color more vibrant by boosting saturation and brightness
    
    Args:
        color: RGB tuple (r, g, b)
        saturation_boost: Factor to increase saturation (1.0 = no change)
        brightness_boost: Factor to increase brightness (1.0 = no change)
    
    Returns:
        tuple: Enhanced RGB color
    """
    r, g, b = color
    
    # Convert RGB to HSV for easier saturation manipulation
    r_norm, g_norm, b_norm = r/255.0, g/255.0, b/255.0
    
    max_val = max(r_norm, g_norm, b_norm)
    min_val = min(r_norm, g_norm, b_norm)
    diff = max_val - min_val
    
    # Calculate HSV values
    # Hue
    if diff == 0:
        h = 0
    elif max_val == r_norm:
        h = (60 * ((g_norm - b_norm) / diff) + 360) % 360
    elif max_val == g_norm:
        h = (60 * ((b_norm - r_norm) / diff) + 120) % 360
    else:
        h = (60 * ((r_norm - g_norm) / diff) + 240) % 360
    
    # Saturation
    s = 0 if max_val == 0 else diff / max_val
    
    # Value (brightness)
    v = max_val
    
    # Boost saturation and brightness
    s = min(s * saturation_boost, 1.0)
    v = min(v * brightness_boost, 1.0)
    
    # Convert back to RGB
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c
    
    if 0 <= h < 60:
        r_prime, g_prime, b_prime = c, x, 0
    elif 60 <= h < 120:
        r_prime, g_prime, b_prime = x, c, 0
    elif 120 <= h < 180:
        r_prime, g_prime, b_prime = 0, c, x
    elif 180 <= h < 240:
        r_prime, g_prime, b_prime = 0, x, c
    elif 240 <= h < 300:
        r_prime, g_prime, b_prime = x, 0, c
    else:
        r_prime, g_prime, b_prime = c, 0, x
    
    # Convert back to 0-255 range
    r_final = int((r_prime + m) * 255)
    g_final = int((g_prime + m) * 255)
    b_final = int((b_prime + m) * 255)
    
    return (r_final, g_final, b_final)

def make_color_glowy(color, lightness_factor=2.4, glow_intensity=0.8):
    """
    Make a color much lighter and glowy by increasing brightness significantly
    
    Args:
        color: RGB tuple (r, g, b)
        lightness_factor: Factor to increase lightness (higher = lighter)
        glow_intensity: How much to preserve the original hue (0-1)
    
    Returns:
        tuple: Glowy RGB color
    """
    r, g, b = color
    
    # Convert to HSV for better control
    r_norm, g_norm, b_norm = r/255.0, g/255.0, b/255.0
    
    max_val = max(r_norm, g_norm, b_norm)
    min_val = min(r_norm, g_norm, b_norm)
    diff = max_val - min_val
    
    # Calculate HSV values
    if diff == 0:
        h = 0
    elif max_val == r_norm:
        h = (60 * ((g_norm - b_norm) / diff) + 360) % 360
    elif max_val == g_norm:
        h = (60 * ((b_norm - r_norm) / diff) + 120) % 360
    else:
        h = (60 * ((r_norm - g_norm) / diff) + 240) % 360
    
    s = 0 if max_val == 0 else diff / max_val
    v = max_val
    
    # Make it glowy: reduce saturation and increase brightness dramatically
    s_glow = s * glow_intensity  # Reduce saturation for glow effect
    v_glow = min(v * lightness_factor, 1.0)  # Increase brightness significantly
    
    # Convert back to RGB
    c = v_glow * s_glow
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v_glow - c
    
    if 0 <= h < 60:
        r_prime, g_prime, b_prime = c, x, 0
    elif 60 <= h < 120:
        r_prime, g_prime, b_prime = x, c, 0
    elif 120 <= h < 180:
        r_prime, g_prime, b_prime = 0, c, x
    elif 180 <= h < 240:
        r_prime, g_prime, b_prime = 0, x, c
    elif 240 <= h < 300:
        r_prime, g_prime, b_prime = x, 0, c
    else:
        r_prime, g_prime, b_prime = c, 0, x
    
    # Convert back to 0-255 range
    r_final = int((r_prime + m) * 255)
    g_final = int((g_prime + m) * 255)
    b_final = int((b_prime + m) * 255)
    
    return (r_final, g_final, b_final)

def create_mythic_album_gif(url, output_path="mythic_animated.gif", frames=60, duration=100, size=(300, 300)):
    """
    Create an animated mythic GIF from an album image URL
    
    Args:
        url: URL of the album image
        output_path: Path to save the GIF (optional)
        frames: Number of frames in the animation (default: 60)
        duration: Duration per frame in milliseconds (default: 100)
        size: Size to resize the image to (default: (300, 300))
    
    Returns:
        PIL.Image: The first frame of the animated GIF (can be saved or further processed)
    """
    # Download and process the image
    response = requests.get(url)
    base_img = Image.open(BytesIO(response.content)).convert("RGBA").resize(size)
    
    # Get the base dominant color
    dominant_color = get_dominant_color(base_img)
    
    animation_frames = []
    
    for i in range(frames):
        # Calculate animation progress (0 to 1)
        progress = i / frames
        
        # Create smooth pulsing effect - one complete breathing cycle
        # Using cosine for smoother start and end
        pulse_main = 0.3 + 0.7 * (0.5 + 0.5 * math.cos(2 * math.pi * progress))  # Range: 0.3 to 1.0
        
        # Create the glowy color (keep it consistent, animate only intensity)
        glowy_color = make_color_glowy(dominant_color, 2.2, 0.6)
        
        # Create the frame with gradient border
        frame = create_mythic_frame_gradient(base_img, glowy_color, pulse_main, progress)
        animation_frames.append(frame)
    
    # Save as animated GIF
    animation_frames[0].save(
        output_path,
        save_all=True,
        append_images=animation_frames[1:],
        duration=duration,
        loop=0,  # Infinite loop
        optimize=True
    )
    
    # Return the first frame (or could return the saved path)
    return animation_frames[0]

def mythic_static(base_img):
    """Create a static mythic effect (for reference/testing)"""
    # Get the glowy color from the album image
    dominant_color = get_dominant_color(base_img)
    glowy_color = make_color_glowy(dominant_color)
    
    # Create glow layer
    glow_layer = Image.new("RGBA", base_img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow_layer)
    center = (base_img.width // 2, base_img.height // 2)
    max_radius = 250

    for r in range(250, 0, -10):
        alpha = int(255 * (r / max_radius) * 0.15)
        draw.ellipse(
            [center[0]-r, center[1]-r, center[0]+r, center[1]+r],
            fill=glowy_color + (alpha,)  # Use glowy color instead of gold
        )

    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(15))
    mythic_img = Image.alpha_composite(glow_layer, base_img)

    # Draw border using glowy color
    frame_draw = ImageDraw.Draw(mythic_img)
    for i in range(6):
        frame_draw.rectangle(
            [i, i, mythic_img.width - i - 1, mythic_img.height - i - 1],
            outline=glowy_color + (255,)  # Use glowy color for border
        )

    return mythic_img

def create_mythic_frame_gradient(base_img, glowy_color, pulse_intensity, progress):
    """Create a single frame with gradient border effect"""
    # Create glow layer with animated intensity
    glow_layer = Image.new("RGBA", base_img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow_layer)
    center = (base_img.width // 2, base_img.height // 2)
    max_radius = 250

    for r in range(250, 0, -10):
        # Vary alpha based on pulse intensity
        base_alpha = 255 * (r / max_radius) * 0.15
        animated_alpha = int(base_alpha * pulse_intensity)
        draw.ellipse(
            [center[0]-r, center[1]-r, center[0]+r, center[1]+r],
            fill=glowy_color + (animated_alpha,)
        )

    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(15))
    mythic_img = Image.alpha_composite(glow_layer, base_img)

    # Draw animated counterclockwise wave border
    frame_draw = ImageDraw.Draw(mythic_img)
    width, height = mythic_img.size
    border_thickness = 12 # Reduced to 2/3 of 16 (about 10.67, rounded to 11)
    
    # Calculate perimeter positions for wave animation
    perimeter = 2 * (width + height - 4)  # Total perimeter length
    wave_length = perimeter / 1.5  # Wave covers 2/3 of perimeter (longer bright area)
    wave_position = (progress * perimeter) % perimeter  # Smooth continuous movement around full perimeter
    
    def get_position_on_perimeter(pos):
        """Convert linear position to (x, y) coordinates on rectangle perimeter"""
        pos = pos % perimeter
        if pos < width:  # Top edge
            return (int(pos), 0)
        elif pos < width + height - 1:  # Right edge
            return (width - 1, int(pos - width + 1))
        elif pos < 2 * width + height - 2:  # Bottom edge
            return (int(width - 1 - (pos - width - height + 1)), height - 1)
        else:  # Left edge
            return (0, int(height - 1 - (pos - 2 * width - height + 2)))
    
    def calculate_wave_intensity(pos):
        """Calculate intensity based on distance from wave center with bright-dark-bright gradient"""
        # Distance from wave center (considering circular nature)
        dist1 = abs(pos - wave_position)
        dist2 = perimeter - dist1  # Distance going the other way around
        distance = min(dist1, dist2)
        
        # Create smooth bright-to-vibrant wave effect
        if distance <= wave_length / 2:
            # Within wave: create smooth gradient using a softer cosine curve
            wave_factor = math.cos(math.pi * distance / (wave_length / 2))
            
            # Create smoother bright-to-vibrant gradient pattern
            if wave_factor >= 0:
                # Bright to medium (center to edges) - smoother transition
                smooth_factor = wave_factor ** 0.7  # Softer curve for positive values
                base_intensity = 0.6  # Higher base for more bright area
                peak_intensity = 1.0   # Very bright peak
                return base_intensity + (peak_intensity - base_intensity) * smooth_factor
            else:
                # Medium to vibrant (very gradual transition)
                smooth_factor = (abs(wave_factor)) ** 1.2  # Even softer transition for negative values
                medium_intensity = 0.6  # Higher medium
                vibrant_intensity = 0.3   # Vibrant but not too dark
                return medium_intensity - (medium_intensity - vibrant_intensity) * smooth_factor
        else:
            return 0.4  # Slightly vibrant base outside wave
    
    # Create border layers with wave animation
    for border_layer in range(border_thickness):
        layer_offset = border_layer * 0.1  # Slight offset for depth
        
        # Draw each side of the border with wave-based intensity
        for side in ['top', 'right', 'bottom', 'left']:
            if side == 'top':
                start_pos = 0
                end_pos = width - 1
                y_coord = border_layer
            elif side == 'right':
                start_pos = width - 1
                end_pos = width + height - 2
                x_coord = width - 1 - border_layer
            elif side == 'bottom':
                start_pos = width + height - 2
                end_pos = 2 * width + height - 3
                y_coord = height - 1 - border_layer
            else:  # left
                start_pos = 2 * width + height - 3
                end_pos = perimeter
                x_coord = border_layer
            
            # Sample multiple points along this side for smooth gradient
            side_length = end_pos - start_pos
            for i in range(max(1, side_length // 4)):  # Sample every 4 pixels for performance
                sample_pos = start_pos + (i * 4) % side_length
                intensity = calculate_wave_intensity(sample_pos + layer_offset)
                
                # Create smoother color transitions
                if intensity < 0.6:
                    # For vibrant areas: smoother transition with gradual saturation boost
                    transition_factor = intensity / 0.6  # Normalize to 0-1 for smooth transition
                    enhanced_factor = 0.7 + transition_factor * 0.5  # Range: 0.7 to 1.2 (smoother)
                    enhanced_glow = 0.3 + transition_factor * 0.3    # Range: 0.3 to 0.6 (gradual saturation)
                    # Gradual saturation boost
                    saturation_boost = 1.8 + transition_factor * 0.4  # Range: 1.8 to 2.2 (smoother boost)
                else:
                    # For bright areas: smooth transition to very bright
                    transition_factor = (intensity - 0.6) / 0.4  # Normalize remaining range
                    enhanced_factor = 1.2 + transition_factor * 2.8  # Range: 1.2 to 4.0 (very bright)
                    enhanced_glow = 0.6 + transition_factor * 0.2    # Range: 0.6 to 0.8 (good saturation)
                    saturation_boost = 1.2 + transition_factor * 0.3  # Range: 1.2 to 1.5 (moderate boost)
                
                # Apply smooth saturation boost for all areas
                vibrant_color = make_color_vibrant(glowy_color, saturation_boost, enhanced_factor)
                border_color = make_color_glowy(vibrant_color, enhanced_factor, enhanced_glow)
                
                alpha = int(255 * (0.8 + intensity * 0.2))  # High visibility: range 80% to 100% alpha
                color = border_color + (alpha,)
                
                # Calculate actual pixel coordinates
                if side == 'top':
                    pixel_x = start_pos + i * 4
                    pixel_y = y_coord
                    # Draw small segment
                    frame_draw.line([(pixel_x, pixel_y), (min(pixel_x + 3, width - 1), pixel_y)], 
                                   fill=color, width=2 if border_layer < 8 else 1)
                elif side == 'right':
                    pixel_x = x_coord
                    pixel_y = start_pos - width + 1 + i * 4
                    frame_draw.line([(pixel_x, pixel_y), (pixel_x, min(pixel_y + 3, height - 1))], 
                                   fill=color, width=2 if border_layer < 8 else 1)
                elif side == 'bottom':
                    pixel_x = width - 1 - (i * 4)
                    pixel_y = y_coord
                    frame_draw.line([(max(pixel_x - 3, 0), pixel_y), (pixel_x, pixel_y)], 
                                   fill=color, width=2 if border_layer < 8 else 1)
                else:  # left
                    pixel_x = x_coord
                    pixel_y = height - 1 - (i * 4)
                    frame_draw.line([(pixel_x, max(pixel_y - 3, 0)), (pixel_x, pixel_y)], 
                                   fill=color, width=2 if border_layer < 8 else 1)

    return mythic_img

def create_mythic_frame(base_img, glowy_color, pulse_intensity):
    """Create a single frame of the mythic effect"""
    # Create glow layer with animated intensity
    glow_layer = Image.new("RGBA", base_img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow_layer)
    center = (base_img.width // 2, base_img.height // 2)
    max_radius = 250

    for r in range(250, 0, -10):
        # Vary alpha based on pulse intensity
        base_alpha = 255 * (r / max_radius) * 0.15
        animated_alpha = int(base_alpha * pulse_intensity)
        draw.ellipse(
            [center[0]-r, center[1]-r, center[0]+r, center[1]+r],
            fill=glowy_color + (animated_alpha,)
        )

    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(15))
    mythic_img = Image.alpha_composite(glow_layer, base_img)

    # Draw animated border
    frame_draw = ImageDraw.Draw(mythic_img)
    border_alpha = int(255 * (0.7 + 0.3 * pulse_intensity))  # Pulsing border opacity
    
    for i in range(6):
        frame_draw.rectangle(
            [i, i, mythic_img.width - i - 1, mythic_img.height - i - 1],
            outline=glowy_color + (border_alpha,)
        )

    return mythic_img

def main(url):
    result_frame = create_mythic_album_gif(
        url=url,
        output_path="mythic_animated.gif",
        frames=60,  # More frames for smoother pulsing animation
        duration=100  # Slightly slower for smoother appearance
    )

    return result_frame


# Example usage
if __name__ == "__main__":
    mystic_url = "https://i.scdn.co/image/ab67616d0000b2734ab2520c2c77a1d66b9ee21d"
    main(mystic_url)