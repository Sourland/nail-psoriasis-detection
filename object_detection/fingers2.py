import os

import cv2
import numpy as np
from mediapipe.tasks import python
from mediapipe.tasks.python import vision
import mediapipe as mp
from .landmarks_constants import *
from .pixel_finder import landmark_to_pixels
from .utils import locate_hand_landmarks, draw_landmarks_on_image, resize_image
import subprocess
import segmentation


def resize_longest_edge_to_target(image, target_size=330):
    height, width, _ = image.shape
    
    # Determine which side is the longest
    if max(height, width) == height:
        new_height = target_size
        aspect_ratio = width / height
        new_width = int(new_height * aspect_ratio)
    else:
        new_width = target_size
        aspect_ratio = height / width
        new_height = int(new_width * aspect_ratio)
    
    resized_image = cv2.resize(image, (new_width, new_height), interpolation=cv2.INTER_LINEAR)
    
    return resized_image


def get_segmentation_mask(image: np.ndarray, threshold: int = 11) -> np.ndarray:
    """
    Generate a binary segmentation mask based on pixel intensity.

    Parameters:
    - image: an RGB image in numpy array format.
    - threshold: pixel values above this threshold will be set to 1, below will be set to 0.

    Returns:
    - Binary segmentation mask as numpy array.
    """

    # Check if the image has three channels
    if len(image.shape) != 3 or image.shape[2] != 3:
        raise ValueError("Expected an RGB image with 3 channels. Received image with shape {}.".format(image.shape))

    # Convert the image to grayscale
    grayscale_image = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)

    # Generate the mask
    mask = np.where(grayscale_image > threshold, 255, 0)

    return mask.astype(np.uint8)
    

def increase_contrast(img):
    """
    Enhance the contrast of an image using CLAHE.

    Args:
    - img (np.ndarray): The input image array.

    Returns:
    - np.ndarray: The contrast-enhanced image array.
    """
    
    # Convert to grayscale (optional, but helps in many scenarios)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    # Apply CLAHE (Contrast Limited Adaptive Histogram Equalization)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    # Merge channels if needed
    if img.shape[-1] == 3:
        enhanced = cv2.merge([enhanced, enhanced, enhanced])
    
    return enhanced


def extract_contour(image: np.ndarray) -> np.ndarray:
    # Find contours
    contours, _ = cv2.findContours(image, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Check if any contours were found
    if not contours:
        raise ValueError("No contours found in the image")

    # Sort the contours by area in descending order
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    
    # Return the largest contour by area
    return np.squeeze(contours[0])



def landmarks_to_pixel_coordinates(image, landmarks) -> list:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return [landmark_to_pixels(gray, landmarks.hand_landmarks[0], idx) for idx in
            range(len(landmarks.hand_landmarks[0]))]


def closest_contour_point(landmarks, contour):

    """
    For each landmark, find the closest left and right points on the contour.

    Args:
    - landmarks (list of tuples): List of landmarks as (x, y) coordinates.
    - contour (numpy array): Contour returned by cv2.findContours.

    Returns:
    - List of tuples, each tuple containing two (x, y) coordinates - the closest left and right points on the contour for each landmark.
    """
    closest_points = []

    for ctr, landmark in enumerate(landmarks):
        if ctr in [THUMB_TIP, INDEX_FINGER_TIP, MIDDLE_FINGER_TIP, RING_FINGER_TIP, PINKY_TIP]:
            left_contour = contour[(contour[:, 0] <= landmark[0]) & (contour[:, 1] >= landmark[1])]
            right_contour = contour[(contour[:, 0] > landmark[0]) & (contour[:, 1] >= landmark[1])]
        else:
            left_contour = contour[(contour[:, 0] <= landmark[0])]
            right_contour = contour[(contour[:, 0] > landmark[0])]


        # For empty contours (when landmarks are on extreme left or right), use the entire contour
        if len(left_contour) == 0:
            left_contour = contour
        if len(right_contour) == 0:
            right_contour = contour

        # Find closest point on the left of the landmark
        left_distances = np.sqrt(np.sum((left_contour - landmark)**2, axis=1))
        left_min_index = np.argmin(left_distances)
        left_closest_point = tuple(left_contour[left_min_index])

        # Find closest point on the right of the landmark
        right_distances = np.sqrt(np.sum((right_contour - landmark)**2, axis=1))
        right_min_index = np.argmin(right_distances)
        right_closest_point = tuple(right_contour[right_min_index])

        closest_points.append((left_closest_point, right_closest_point))

    return closest_points


def draw_landmarks_and_connections(image, landmarks, closest_points):
    """
    Draw landmarks, closest left and right points on the contour, and lines connecting them on the image.

    Args:
    - image (numpy array): The image on which to draw.
    - landmarks (list of tuples): List of landmarks as (x, y) coordinates.
    - contour (numpy array): Contour returned by cv2.findContours.
    """
    # Draw landmarks as red circles
    for landmark in landmarks:
        cv2.circle(image, tuple(map(int, landmark)), 3, (0, 0, 255), -1)

    # Draw closest contour points as blue circles and lines connecting them in green
    for landmark, (left_closest_point, right_closest_point) in zip(landmarks, closest_points):
        cv2.circle(image, tuple(map(int, left_closest_point)), 3, (255, 0, 0), -1)
        cv2.circle(image, tuple(map(int, right_closest_point)), 3, (255, 0, 0), -1)
        cv2.line(image, tuple(map(int, landmark)), tuple(map(int, left_closest_point)), (0, 255, 0), 1)
        cv2.line(image, tuple(map(int, landmark)), tuple(map(int, right_closest_point)), (0, 255, 0), 1)

    return image


def get_bounding_box(image: np.ndarray, points: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray, float]:
    """Draw a rotated bounding box around the given points on the image."""
    # Convert the list of points to a suitable format for cv2.minAreaRect
    rect_points = np.array(points).reshape(-1, 1, 2).astype(int)

    # Compute the rotated bounding box
    rect = cv2.minAreaRect(rect_points)
    (center, (width, height), theta) = rect

    # Ensure the rectangle is in portrait orientation
    if width > height:
        width, height = height, width
        theta -= 90  # Adjust the rotation

    return (center, (width, height), theta)


def extract_roi(image, rect):
    """
    Extracts and returns the region of interest inside the rectangle.

    :param image: The original image.
    :param rect: A tuple that contains the center (x, y), size (width, height), and angle of the rectangle.
    :param padding: The amount of padding to add to each side of the image.
    :return: The extracted region of interest and the rotation matrix.
    """

    # Scale up the width and height by 15% for a margin.
    center, size, theta = rect
    width, height = size
    width += width * 0.15
    height += height * 0.15

    # Obtain the rotation matrix
    rotation_matrix = cv2.getRotationMatrix2D(center, theta, 1)

    # Perform the affine transformation on the padded image
    warped_image = cv2.warpAffine(image, rotation_matrix, (image.shape[1], image.shape[0]), borderValue=(0, 0, 0))

    # Extract the ROI
    x, y = int(center[0] - width // 2), int(center[1] - height // 2)
    roi = warped_image[y:y + int(height), x:x + int(width)]

    return roi, rotation_matrix


def transform_point(point, matrix):
    # Convert point to homogeneous coordinates
    homogeneous_point = np.array([[point[0]], [point[1]], [1]])
    
    # Apply affine transformation
    transformed_point = np.squeeze(np.dot(matrix, homogeneous_point))
    
    # Return the transformed point in (x, y) format
    return (int(transformed_point[0]), int(transformed_point[1]))


def adjust_for_roi_crop(point, roi_center, roi_size):
    """
    Adjust the point coordinates based on the ROI cropping.

    Args:
    - point (tuple): The (x, y) coordinates of the point.
    - roi_center (tuple): The (x, y) coordinates of the center of the ROI.
    - roi_size (tuple): The (width, height) size dimensions of the ROI.

    Returns:
    - tuple: The adjusted (x, y) coordinates of the point within the cropped region.
    """
    x_offset = int(roi_center[0] - roi_size[0] // 2)
    y_offset = int(roi_center[1] - roi_size[1] // 2)
    
    adjusted_x = point[0] - x_offset
    adjusted_y = point[1] - y_offset
    return np.array([adjusted_x, adjusted_y])


def process_image(PATH, MASKS_OUTPUT_DIR, FINGER_OUTPUT_DIR, NAIL_OUTPUT_DIR):
    image,  landmarks = locate_hand_landmarks(PATH, "hand_landmarker.task")
    padding = 200
    if not landmarks.hand_landmarks:
        print(f"Warning: No landmarks detected for {os.path.basename(PATH)}. Skipping save operation.")
        return
    else:
        landmarks = landmarks
    landmark_pixels = landmarks_to_pixel_coordinates(image, landmarks)
    enhanced_image = increase_contrast(image)
    result =  segmentation.bg.remove(data=enhanced_image)
    seg_mask = get_segmentation_mask(result)
    contour = extract_contour(seg_mask)
    rgb_mask = cv2.cvtColor(seg_mask, cv2.COLOR_GRAY2RGB)
    closest_points = closest_contour_point(landmark_pixels, contour)
    image = cv2.copyMakeBorder(image, padding, padding, padding, padding, cv2.BORDER_CONSTANT, value=[0, 0, 0])
    for key in ['INDEX', 'MIDDLE', 'RING', 'PINKY']:
        finger_roi_points = [item for idx in landmarks_per_finger[key][1:] for item in closest_points[idx]]
        finger_roi_points.append(landmark_pixels[landmarks_per_finger[key][0]])
        rect = get_bounding_box(rgb_mask, finger_roi_points)
        roi, rotation_matrix = extract_roi(rgb_mask, rect)
        dip = np.array(landmark_pixels[landmarks_per_finger[key][1]])
        pip = np.array(landmark_pixels[landmarks_per_finger[key][2]])

        # Rotate the landmarks
        rotated_pip = transform_point(pip, rotation_matrix)
        rotated_dip = transform_point(dip, rotation_matrix)

        # Map the landmarks to the resized image
        new_pip = adjust_for_roi_crop(rotated_pip, rect[0], rect[1])
        new_dip = adjust_for_roi_crop(rotated_dip, rect[0], rect[1])
        # Draw the landmarks on the resized image
        cv2.circle(roi, new_pip, 3, (0, 0, 255), -1)
        cv2.circle(roi, new_dip, 3, (0, 0, 255), -1)
        OUTPUT_PATH_FINGER = os.path.join(FINGER_OUTPUT_DIR, key + os.path.basename(PATH))
        if roi.size > 0 and roi is not None:
            cv2.imwrite(OUTPUT_PATH_FINGER, roi)
        else:
            print(f"Warning: The ROI image is empty or None. Skipping save operation for finger {os.path.basename(OUTPUT_PATH_FINGER)}.")

    
    OUTPUT_PATH_MASK = os.path.join(MASKS_OUTPUT_DIR, "seg_" + os.path.basename(PATH))
    cv2.imwrite(OUTPUT_PATH_MASK, rgb_mask)

    


# if __name__ == "__main__":
#     DIR_PATH = "dataset/hands/swolen/"
#     OUTPUT_DIR = "results/LandmarkPics/"

#     # Make sure the output directory exists
#     if not os.path.exists(OUTPUT_DIR):
#         os.makedirs(OUTPUT_DIR)

#     for image_name in os.listdir(DIR_PATH):
#         if image_name.endswith(('.jpg', '.jpeg', '.png')):  # checking file extension
#             image_path = os.path.join(DIR_PATH, image_name)
#             process_image(image_path, OUTPUT_DIR)

