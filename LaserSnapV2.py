# ------------------------------
# 1. IMPORTS
# ------------------------------
# Standard library imports
import os
import shutil
import time
from datetime import datetime
import csv
import threading
from pathlib import Path

# Third-party imports
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import pyvisa
import serial.tools.list_ports
import spectral.io.envi as envi
import spectral as spy
from PIL import Image, ImageTk, ImageOps
import logging
import numpy as np

# ------------------------------
# 2. CONSTANTS AND GLOBAL VARIABLES
# ------------------------------
# Paths and storage
SAVED_IMAGES_DIRECTORY = r'C:\BaySpec\GoldenEye\saved_images'

# Data storage
before_snapshot = []
loaded_cubes = []
loaded_images = []
selected_images = []
available_wavelengths = set()

# Project information
experiment_finished = False
project_name = ""
output_path = ""
raw_data_folder = ""  # New: folder where Golden Eye saves raw data

# Device status
tls_found = False
golden_eye_found = False
tls_device_address = None
arduino_port = None
trigger_string = 'trigger\n'

# Acquisition monitoring
acquisition_log = []  # New: tracks acquisition progress
file_timeout = 60  # New: timeout in seconds for file creation
acquisition_log_path = ""  # New: path to current acquisition log

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')


# ------------------------------
# 3. UTILITY FUNCTIONS
# ------------------------------
def sort_folders_by_modification(folders):
    folders_with_time = [(folder, os.path.getmtime(os.path.join(SAVED_IMAGES_DIRECTORY, folder))) for folder in folders]
    sorted_folders = sorted(folders_with_time, key=lambda x: x[1])
    return [folder[0] for folder in sorted_folders]


def create_acquisition_log_file():
    """Create a CSV log file for tracking acquisitions"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = f"acquisition_log_{project_name}_{timestamp}.csv"
    log_path = os.path.join(output_path, log_filename)

    with open(log_path, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['Index', 'Wavelength', 'Picture_Number', 'Expected_Name',
                         'Raw_Filename', 'Status', 'Timestamp', 'File_Size_Bytes'])

    return log_path


def update_acquisition_log(log_path, index, wavelength, pic_num, expected_name,
                           raw_filename='', status='pending', file_size=0):
    """Update the acquisition log with new information
    Status can be: pending, completed, timeout, cancelled"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Check if entry exists
    existing_data = []
    if os.path.exists(log_path):
        with open(log_path, 'r', newline='') as csvfile:
            reader = csv.reader(csvfile)
            existing_data = list(reader)

    # Update or append entry
    entry_found = False
    for i, row in enumerate(existing_data[1:], 1):  # Skip header
        if len(row) > 0 and int(row[0]) == index:
            existing_data[i] = [index, wavelength, pic_num, expected_name,
                                raw_filename, status, timestamp, file_size]
            entry_found = True
            break

    if not entry_found and existing_data:
        existing_data.append([index, wavelength, pic_num, expected_name,
                              raw_filename, status, timestamp, file_size])

    # Write back to file
    with open(log_path, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerows(existing_data)


def wait_for_new_file(timeout_seconds=60):
    """Wait for a new file to be created in the raw data folder"""
    if not raw_data_folder or not os.path.exists(raw_data_folder):
        logging.error("Raw data folder not set or doesn't exist")
        return None

    # Get initial list of files
    initial_files = set(os.listdir(raw_data_folder))
    start_time = time.time()

    logging.info(f"Waiting for new file in {raw_data_folder}...")

    while time.time() - start_time < timeout_seconds:
        try:
            current_files = set(os.listdir(raw_data_folder))
            new_files = current_files - initial_files

            # Look for new .bin files
            for new_file in new_files:
                if new_file.endswith('.bin'):
                    file_path = os.path.join(raw_data_folder, new_file)
                    file_size = os.path.getsize(file_path)

                    # Wait a bit to ensure file is fully written
                    time.sleep(2)

                    # Check if file size is stable
                    new_size = os.path.getsize(file_path)
                    if new_size == file_size and file_size > 0:
                        logging.info(f"New file detected: {new_file} (size: {file_size} bytes)")
                        return new_file

            time.sleep(1)  # Check every second

        except Exception as e:
            logging.error(f"Error checking for new files: {e}")
            time.sleep(1)

    # Timeout reached
    logging.warning(f"Timeout: No new file detected in {timeout_seconds} seconds")
    return None


def update_status_label(message):
    """Update the acquisition status label in the UI"""
    if 'acquisition_status_label' in globals():
        root.after(0, lambda: acquisition_status_label.config(text=message))


def load_acquisition_from_csv():
    """Load incomplete acquisitions from a CSV file and populate the grid"""
    # Ask user to select CSV file
    csv_file = filedialog.askopenfilename(
        title="Select Acquisition Log CSV",
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
    )

    if not csv_file:
        return

    try:
        # Clear existing rows in the tree
        for item in tree.get_children():
            tree.delete(item)

        # Read CSV and group by wavelength
        wavelength_dict = {}

        with open(csv_file, 'r', newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                # Only load acquisitions that are not completed (pending, timeout, or cancelled)
                if row['Status'] in ['pending', 'timeout', 'cancelled']:
                    wavelength = row['Wavelength']
                    if wavelength not in wavelength_dict:
                        wavelength_dict[wavelength] = 0
                    wavelength_dict[wavelength] += 1

        # Populate the tree with grouped data
        if wavelength_dict:
            for wavelength, count in wavelength_dict.items():
                tree.insert("", "end", values=(wavelength, count))

            messagebox.showinfo("Success",
                                f"Loaded {sum(wavelength_dict.values())} incomplete acquisitions from {len(wavelength_dict)} wavelengths")
        else:
            messagebox.showinfo("No Incomplete Acquisitions",
                                "All acquisitions in the selected file are completed.")

    except Exception as e:
        logging.error(f"Error loading CSV file: {e}")
        messagebox.showerror("Error", f"Failed to load CSV file: {str(e)}")


def select_raw_data_folder():
    """Prompt user to select the folder where Golden Eye saves raw data"""
    global raw_data_folder

    folder = filedialog.askdirectory(
        title="Select Golden Eye Raw Data Folder",
        initialdir=raw_data_folder if raw_data_folder else "C:\\"
    )

    if folder:
        raw_data_folder = folder
        raw_folder_label.config(text=f"Raw folder: {folder}")
        logging.info(f"Raw data folder selected: {folder}")
        check_device_status()
    else:
        messagebox.showwarning("Warning", "Raw data folder must be selected before starting acquisition")


def load_folder():
    folder_path = filedialog.askdirectory()
    if folder_path:
        logging.info(f"Folder selected: {folder_path}")
        process_folder(folder_path)


def process_folder(folder_path):
    # Clear previous images
    for widget in image_panel_frame.winfo_children():
        widget.destroy()

    # Clear previous cubes, selections, and wavelengths
    loaded_cubes.clear()
    selected_images.clear()
    loaded_images.clear()
    sum_cubes_button.config(state="disabled")
    average_cubes_button.config(state="disabled")
    view_selected_button.config(state="disabled")
    available_wavelengths.clear()

    subfolders = [f.path for f in os.scandir(folder_path) if f.is_dir()]
    total_subfolders = len(subfolders)

    if total_subfolders == 0:
        logging.warning("No subfolders found in the selected folder.")
        progress_label.config(text="Loaded 0 of 0 subfolders")
        return

    logging.info(f"Found {total_subfolders} subfolders.")

    loaded_folders = 0  # Track the number of folders processed

    # Create a frame that will contain the grid of images
    image_grid_frame = tk.Frame(image_panel_frame)
    image_grid_frame.pack(fill=tk.BOTH, expand=True)

    # Number of columns in the grid
    num_columns = 4  # Adjust this based on your preferred layout
    current_row = 0
    current_column = 0

    # Loop through each subfolder and process the hyperspectral images
    for subfolder in subfolders:
        folder_name = os.path.basename(subfolder)
        parts = folder_name.split('_')

        if len(parts) >= 3:
            wavelength = parts[2]  # Extract wavelength from the folder name
            i = parts[3] if len(parts) > 3 else "1"  # Extract i or default to 1

            hdr_path = os.path.join(subfolder, 'spectral_image_processed_image.hdr')
            bin_path = os.path.join(subfolder, 'spectral_image_processed_image.bin')

            if os.path.exists(hdr_path) and os.path.exists(bin_path):
                logging.info(f"Loading hyperspectral cube from: {hdr_path} and {bin_path}")
                try:
                    # Load the cube using spectral.io.envi
                    meta_cube = envi.open(hdr_path, bin_path)
                    cube = meta_cube.load()

                    # Define the RGB bands
                    rgb_bands = (29, 19, 9)  # Adjust these bands as needed

                    # Save the RGB image
                    output_rgb_image = os.path.join(subfolder, 'rgb_image.png')
                    spy.save_rgb(output_rgb_image, cube, rgb_bands)
                    logging.info(f"RGB image saved at: {output_rgb_image}")

                    # Store the cube data and metadata, along with the path to the RGB image
                    loaded_cubes.append((cube, meta_cube.metadata, wavelength, i, output_rgb_image))
                    available_wavelengths.add(wavelength)  # Track unique wavelengths

                    # Display the image
                    img = Image.open(output_rgb_image)
                    img = img.resize((200, 150), Image.Resampling.LANCZOS)  # Slightly smaller for grid layout
                    img_tk = ImageTk.PhotoImage(img)

                    # Store the image to prevent garbage collection
                    loaded_images.append(img_tk)

                    # Create a frame for each image, its label, and checkbox
                    image_frame = tk.Frame(image_grid_frame)
                    image_frame.grid(row=current_row, column=current_column, padx=5, pady=5, sticky='nsew')

                    # Display the image in the frame
                    img_label = tk.Label(image_frame, image=img_tk)
                    img_label.pack()

                    # Create a variable to track the checkbox state
                    checkbox_var = tk.BooleanVar()

                    # Create a checkbox next to the image name and make it selectable
                    checkbox = tk.Checkbutton(image_frame, text=f'{wavelength}_{i}', variable=checkbox_var,
                                              onvalue=True, offvalue=False,
                                              command=lambda idx=len(loaded_cubes) - 1,
                                                             var=checkbox_var: toggle_image_selection(idx, var))
                    checkbox.pack(pady=5)

                    # Update grid position for next image
                    current_column += 1
                    if current_column >= num_columns:
                        current_column = 0
                        current_row += 1

                    # Update the progress after each subfolder is processed
                    loaded_folders += 1
                    progress_label.config(text=f"Loaded {loaded_folders} of {total_subfolders} subfolders")
                    root.update_idletasks()

                except Exception as e:
                    logging.error(f"Error loading or processing cube: {e}")
            else:
                logging.warning(f"Hyperspectral files not found in {subfolder}")

    # Final update to the progress label in case all subfolders were processed
    progress_label.config(text=f"Loaded {loaded_folders} of {total_subfolders} subfolders")

    # Update the wavelength filter dropdown with the available wavelengths
    update_wavelength_filters()


# ------------------------------
# 4. DEVICE COMMUNICATION FUNCTIONS
# ------------------------------
def check_tls_device():
    try:
        rm = pyvisa.ResourceManager()
        resources = rm.list_resources()
        logging.info(f"VISA Resources found: {resources}")
        if not resources:
            logging.info("No VISA resources found.")
            return False, None
        print('check')
        for resource in resources:
            try:
                device = rm.open_resource(resource)
                logging.info(f"Device Query: {device.query('*IDN?')}")
                print(device.query('*IDN?'))
                if "CS130B" in device.query('*IDN?'):
                    logging.info(f"TLS device found at {resource}")
                    return True, resource
            except pyvisa.VisaIOError:
                continue

        logging.info("TLS device not found.")
        return False, None

    except pyvisa.VisaIOError as e:
        logging.error(f"Error accessing VISA resources: {e}")
        return False, None


def check_arduino_device():
    try:
        ports = list(serial.tools.list_ports.comports())
        logging.info(f"Available serial ports: {ports}")
        if not ports:
            logging.info("No serial ports found.")
            return False, None

        for port in ports:
            logging.info(f"Port Description: {port.description}")
            if "Arduino" in port.description or "CP210" in port.description:
                logging.info(f"Arduino found at {port.device}")
                return True, port.device

        logging.info("Arduino device not found.")
        return False, None

    except Exception as e:
        logging.error(f"Error accessing serial ports: {e}")
        return False, None


def find_tls():
    global tls_found, tls_device_address
    tls_found, tls_device_address = check_tls_device()

    if tls_found:
        tls_status_label.config(bg='green')
        find_tls_button.config(state='disabled')
        check_device_status()
    else:
        tls_status_label.config(bg='red')
        messagebox.showerror("Error", "TLS device not found")


def find_golden_eye():
    global golden_eye_found, arduino_port
    golden_eye_found, arduino_port = check_arduino_device()

    if golden_eye_found:
        golden_eye_status_label.config(bg='green')
        find_golden_eye_button.config(state='disabled')
        check_device_status()
    else:
        golden_eye_status_label.config(bg='red')
        messagebox.showerror("Error", "Golden Eye (Arduino) device not found")


def check_device_status():
    if tls_found and golden_eye_found and raw_data_folder:
        execute_button.config(state='normal')
        load_csv_button.config(state='normal')


def send_trigger():
    baud_rate = 9600
    with serial.Serial(arduino_port, baud_rate, timeout=1) as ser:
        time.sleep(2)
        ser.write(trigger_string.encode('utf-8'))
        logging.info(f"Sent: {trigger_string.strip()}")


# ------------------------------
# 5. DATA PROCESSING FUNCTIONS
# ------------------------------
def sum_selected_cubes():
    if not selected_images:
        messagebox.showerror("Error", "No images selected for summing.")
        return

    combined_cube = None
    first_hdr_metadata = None
    rgb_bands = (29, 19, 9)  # Example of RGB bands

    for idx in selected_images:
        cube_data, cube_metadata, wavelength, i, _ = loaded_cubes[idx]

        logging.info(f"Summing cube for {wavelength}_{i}")

        # Sum the cubes
        if combined_cube is None:
            combined_cube = cube_data
            first_hdr_metadata = cube_metadata
        else:
            # Ensure the cubes have the same dimensions
            assert combined_cube.shape == cube_data.shape, "Cubes must have the same dimensions for summing."
            combined_cube += cube_data

    if combined_cube is not None:
        # Save the summed RGB image temporarily
        summed_rgb_image = os.path.join(SAVED_IMAGES_DIRECTORY, 'summed_rgb_image.png')
        spy.save_rgb(summed_rgb_image, combined_cube, rgb_bands)
        logging.info(f"Summed RGB image saved at: {summed_rgb_image}")

        # Show the combined image in a popup window and provide Save options
        show_combined_image_popup(summed_rgb_image, combined_cube, first_hdr_metadata)
    else:
        messagebox.showerror("Error", "Could not sum the selected cubes.")


def average_selected_cubes():
    if not selected_images:
        messagebox.showerror("Error", "No images selected for averaging.")
        return

    combined_cube = None
    first_hdr_metadata = None
    rgb_bands = (29, 19, 9)  # Example of RGB bands
    cube_count = 0

    for idx in selected_images:
        cube_data, cube_metadata, wavelength, i, _ = loaded_cubes[idx]

        logging.info(f"Including cube for {wavelength}_{i} in average")

        # Add the cubes
        if combined_cube is None:
            combined_cube = cube_data.copy()  # Use copy to avoid modifying original
            first_hdr_metadata = cube_metadata
        else:
            # Ensure the cubes have the same dimensions
            assert combined_cube.shape == cube_data.shape, "Cubes must have the same dimensions for averaging."
            combined_cube += cube_data

        cube_count += 1

    if combined_cube is not None and cube_count > 0:
        # Divide by the number of cubes to get the average
        combined_cube = combined_cube / cube_count

        # Save the averaged RGB image temporarily
        averaged_rgb_image = os.path.join(SAVED_IMAGES_DIRECTORY, 'averaged_rgb_image.png')
        spy.save_rgb(averaged_rgb_image, combined_cube, rgb_bands)
        logging.info(f"Averaged RGB image saved at: {averaged_rgb_image}")

        # Show the combined image in a popup window and provide Save options
        show_averaged_image_popup(averaged_rgb_image, combined_cube, first_hdr_metadata)
    else:
        messagebox.showerror("Error", "Could not average the selected cubes.")


def add_cubes_for_same_wavelength(folders):
    date_str = datetime.now().strftime("%m-%d")

    wavelength_dict = {}
    for folder in folders:
        parts = folder.split('_')
        if len(parts) >= 3:
            wavelength = parts[2]
            if wavelength not in wavelength_dict:
                wavelength_dict[wavelength] = []
            wavelength_dict[wavelength].append(folder)

    for wavelength, folders in wavelength_dict.items():
        combined_cube = None
        first_hdr_metadata = None

        for folder in folders:
            hdr_path = os.path.join(SAVED_IMAGES_DIRECTORY, folder, 'spectral_image_processed_image.hdr')
            bin_path = os.path.join(SAVED_IMAGES_DIRECTORY, folder, 'spectral_image_processed_image.bin')

            cube = envi.open(hdr_path, bin_path)
            cube_data = cube.load()

            if first_hdr_metadata is None:
                first_hdr_metadata = cube.metadata

            if combined_cube is None:
                combined_cube = cube_data
            else:
                assert combined_cube.shape == cube_data.shape, f"Cubes must have the same dimensions: {folder}"
                combined_cube += cube_data

        rgb_bands = (29, 19, 9)
        output_rgb_file = os.path.join(output_path, f'{project_name}_{date_str}_{wavelength}_combined.png')
        spy.save_rgb(output_rgb_file, combined_cube, rgb_bands)
        logging.info(f"Saved combined RGB image for wavelength {wavelength} at {output_rgb_file}")

        output_hdr_file = os.path.join(output_path, f'{project_name}_{date_str}_{wavelength}_union.hdr')
        envi.save_image(output_hdr_file, combined_cube, metadata=first_hdr_metadata, force=True)
        logging.info(f"Saved combined cube for wavelength {wavelength} at {output_hdr_file}")


def save_rgb(image_path):
    directory = filedialog.askdirectory()
    if not directory:
        return  # No directory selected

    # Create the new file path
    rgb_save_path = os.path.join(directory, "summed_rgb_image.png")

    try:
        shutil.copy(image_path, rgb_save_path)
        messagebox.showinfo("Success", f"RGB image saved at: {rgb_save_path}")
    except Exception as e:
        logging.error(f"Failed to save RGB image: {e}")
        messagebox.showerror("Error", f"Failed to save RGB image: {e}")


def save_rgb_image(image_path, default_filename):
    directory = filedialog.askdirectory()
    if not directory:
        return  # No directory selected

    # Create the new file path
    rgb_save_path = os.path.join(directory, default_filename)

    try:
        shutil.copy(image_path, rgb_save_path)
        messagebox.showinfo("Success", f"RGB image saved at: {rgb_save_path}")
    except Exception as e:
        logging.error(f"Failed to save RGB image: {e}")
        messagebox.showerror("Error", f"Failed to save RGB image: {e}")


def save_cube(summed_cube, metadata):
    # Ask the user to select a directory to save the hyperspectral cube
    directory = filedialog.askdirectory()
    if not directory:
        return  # No directory selected

    hdr_save_path = os.path.join(directory, "summed_cube.hdr")
    bin_save_path = os.path.join(directory, "summed_cube.bin")

    try:
        # Save the hyperspectral cube using spectral.io.envi
        envi.save_image(hdr_save_path, summed_cube, metadata=metadata, force=True)
        messagebox.showinfo("Success", f"Summed cube saved at: {hdr_save_path}")
    except Exception as e:
        logging.error(f"Failed to save hyperspectral cube: {e}")
        messagebox.showerror("Error", f"Failed to save hyperspectral cube: {e}")


def save_averaged_cube(averaged_cube, metadata):
    directory = filedialog.askdirectory()
    if not directory:
        return  # No directory selected

    hdr_save_path = os.path.join(directory, "averaged_cube.hdr")

    try:
        # Save the hyperspectral cube using spectral.io.envi
        envi.save_image(hdr_save_path, averaged_cube, metadata=metadata, force=True)
        messagebox.showinfo("Success", f"Averaged cube saved at: {hdr_save_path}")
    except Exception as e:
        logging.error(f"Failed to save hyperspectral cube: {e}")
        messagebox.showerror("Error", f"Failed to save hyperspectral cube: {e}")


# ------------------------------
# 6. UI EVENT HANDLERS
# ------------------------------
def execute_commands():
    global experiment_finished, acquisition_log, acquisition_log_path, project_name, output_path

    # Check if raw data folder is selected
    if not raw_data_folder:
        messagebox.showerror("Error", "Please select the Golden Eye raw data folder first!")
        return

    # Check if project name and output path are set
    if not project_name or not output_path:
        # Create a simple dialog to get project info
        dialog = tk.Toplevel(root)
        dialog.title("Project Information")
        dialog.geometry("400x200")
        dialog.transient(root)
        dialog.grab_set()

        tk.Label(dialog, text="Project Name:").pack(pady=5)
        name_entry = tk.Entry(dialog, width=40)
        name_entry.pack(pady=5)

        tk.Label(dialog, text="Output Path:").pack(pady=5)
        path_label = tk.Label(dialog, text="No folder selected", relief=tk.SUNKEN, width=40)
        path_label.pack(pady=5)

        def select_folder():
            folder = filedialog.askdirectory()
            if folder:
                path_label.config(text=folder)
                dialog.selected_path = folder

        dialog.selected_path = ""
        tk.Button(dialog, text="Browse", command=select_folder).pack(pady=5)

        def save_and_continue():
            global project_name, output_path
            project_name = name_entry.get()
            output_path = dialog.selected_path

            if not project_name or not output_path:
                messagebox.showerror("Error", "Both project name and output path are required!")
                return

            if not os.path.exists(output_path):
                os.makedirs(output_path)

            dialog.destroy()

        tk.Button(dialog, text="Continue", command=save_and_continue).pack(pady=10)

        # Wait for dialog to close
        root.wait_window(dialog)

        # Check again if values were set
        if not project_name or not output_path:
            return

    # Initialize acquisition tracking
    acquisition_log = []

    # Create log file
    acquisition_log_path = create_acquisition_log_file()

    # Build acquisition plan and pre-populate log
    acquisition_index = 0
    for child in tree.get_children():
        wavelength = tree.item(child)["values"][0]
        num_pictures = int(tree.item(child)["values"][1])

        for i in range(1, num_pictures + 1):
            expected_name = f"{project_name}_{wavelength}_{i}"
            acquisition_log.append({
                'index': acquisition_index,
                'wavelength': wavelength,
                'pic_num': i,
                'expected_name': expected_name
            })

            # Pre-populate log file
            update_acquisition_log(acquisition_log_path, acquisition_index, wavelength, i,
                                   expected_name, '', 'pending', 0)
            acquisition_index += 1

    # Start acquisition
    rm = pyvisa.ResourceManager()
    device = rm.open_resource(tls_device_address)
    device.timeout = 6000
    take_snapshot()

    # Execute acquisition commands sequentially
    for entry in acquisition_log:
        wavelength = entry['wavelength']
        pic_num = entry['pic_num']
        index = entry['index']

        # Update status
        update_status_label(f"Acquiring: {wavelength}nm #{pic_num} ({index + 1}/{len(acquisition_log)})")

        # Send wavelength command
        device.write(f'gowave {wavelength}')
        logging.info(f"TLS Command Sent: gowave {wavelength}")
        time.sleep(5)

        # Send trigger
        send_trigger()
        logging.info(f"Arduino Triggered for {wavelength}nm picture {pic_num}")

        # Wait for the new file
        new_file = wait_for_new_file(file_timeout)

        if new_file:
            # Update log with successful acquisition
            file_path = os.path.join(raw_data_folder, new_file)
            file_size = os.path.getsize(file_path)

            update_acquisition_log(
                acquisition_log_path,
                index,
                wavelength,
                pic_num,
                entry['expected_name'],
                new_file,
                'completed',
                file_size
            )

            update_status_label(f"Completed: {wavelength}nm #{pic_num} -> {new_file}")
        else:
            # Timeout occurred
            update_acquisition_log(
                acquisition_log_path,
                index,
                wavelength,
                pic_num,
                entry['expected_name'],
                '',
                'timeout',
                0
            )

            # Ask user if they want to continue
            result = messagebox.askyesno(
                "Acquisition Timeout",
                f"No file detected for {wavelength}nm #{pic_num}.\n"
                f"Do you want to continue with the next acquisition?"
            )

            if not result:
                # Mark remaining as cancelled
                for remaining_entry in acquisition_log[index + 1:]:
                    update_acquisition_log(
                        acquisition_log_path,
                        remaining_entry['index'],
                        remaining_entry['wavelength'],
                        remaining_entry['pic_num'],
                        remaining_entry['expected_name'],
                        '',
                        'cancelled',
                        0
                    )
                break

    experiment_finished = True
    process_button.config(state='normal')

    # Final status update
    update_status_label("Acquisition complete!")


def resume_acquisition():
    """Resume a previous incomplete acquisition"""
    global output_path

    # Ask user to select the project folder
    folder = filedialog.askdirectory(title="Select Project Folder with Previous Acquisition")
    if not folder:
        return

    output_path = folder

    # Check for previous acquisition
    prev_acq = check_previous_acquisition()

    if not prev_acq:
        messagebox.showinfo("No Previous Acquisition",
                            "No incomplete acquisition found in the selected folder.")
        return

    # Show resume dialog
    result = messagebox.askyesno(
        "Resume Acquisition",
        f"Found incomplete acquisition:\n"
        f"Total: {prev_acq['total']} acquisitions\n"
        f"Completed: {prev_acq['completed']}\n"
        f"Remaining: {prev_acq['incomplete']}\n\n"
        f"Resume from where it stopped?"
    )

    if result:
        # Resume from log
        resume_from_log(prev_acq['log_path'])

        # Execute remaining acquisitions
        execute_resumed_commands()


def execute_resumed_commands():
    """Execute commands for resumed acquisition"""
    global experiment_finished, monitoring_thread, stop_monitoring
    global current_acquisition_index, last_file_time

    # Check if raw data folder is selected
    if not raw_data_folder:
        messagebox.showerror("Error", "Please select the Golden Eye raw data folder first!")
        return

    stop_monitoring = False

    # Start file monitoring thread
    monitoring_thread = threading.Thread(target=monitor_raw_files, daemon=True)
    monitoring_thread.start()

    # Start acquisition
    rm = pyvisa.ResourceManager()
    device = rm.open_resource(tls_device_address)
    device.timeout = 6000

    # Execute remaining acquisition commands
    for i, entry in enumerate(acquisition_log):
        wavelength = entry['wavelength']
        pic_num = entry['pic_num']

        update_status_label(f"Acquiring: {wavelength}nm #{pic_num} ({i + 1}/{len(acquisition_log)})")

        device.write(f'gowave {wavelength}')
        logging.info(f"TLS Command Sent: gowave {wavelength}")
        time.sleep(5)

        send_trigger()
        logging.info(f"Arduino Triggered for {wavelength}nm picture {pic_num}")

        current_acquisition_index = i
        time.sleep(20)  # Wait for acquisition

    experiment_finished = True
    stop_monitoring = True
    process_button.config(state='normal')

    # Final status update
    update_status_label("Resumed acquisition complete!")


def process_results():
    if not experiment_finished:
        messagebox.showerror("Error", "Experiment is not finished yet!")
        return

    after_snapshot = os.listdir(SAVED_IMAGES_DIRECTORY)
    logging.info(f"New snapshot taken: {after_snapshot}")

    new_folders = list(set(after_snapshot) - set(before_snapshot))
    new_folders_sorted = sort_folders_by_modification(new_folders)
    logging.info(f"Sorted new folders: {new_folders_sorted}")

    total_pictures = sum(int(tree.item(child)["values"][1]) for child in tree.get_children())
    logging.info(f"Total pictures expected: {total_pictures}")

    if len(new_folders_sorted) == total_pictures:
        open_project_window(new_folders_sorted)
        add_cubes_for_same_wavelength(new_folders_sorted)
    else:
        messagebox.showerror("Error",
                             f"Expected {total_pictures} folders, but found {len(new_folders_sorted)} new folders.")


def add_row():
    wavelength = wavelength_entry.get()
    num_pictures = pictures_entry.get()

    # Validate inputs
    if not wavelength or not num_pictures:
        messagebox.showerror("Error", "Both wavelength and number of pictures are required.")
        return

    # Add the row to the treeview
    tree.insert("", "end", values=(wavelength, num_pictures))

    # Clear entry fields after adding
    wavelength_entry.delete(0, tk.END)
    pictures_entry.delete(0, tk.END)


def edit_selected_row():
    """Edit the currently selected row"""
    selected_items = tree.selection()
    if not selected_items:
        messagebox.showwarning("Warning", "Please select a row to edit.")
        return

    # Get the values from the selected row
    item_id = selected_items[0]
    current_values = tree.item(item_id, "values")

    # Create a dialog for editing
    edit_dialog = tk.Toplevel(root)
    edit_dialog.title("Edit Row")
    edit_dialog.geometry("300x150")
    edit_dialog.transient(root)  # Make it modal
    edit_dialog.grab_set()

    # Input fields for wavelength and number of pictures
    tk.Label(edit_dialog, text="Wavelength (nm):").pack(pady=(10, 5))
    wavelength_edit = tk.Entry(edit_dialog)
    wavelength_edit.insert(0, current_values[0])
    wavelength_edit.pack(pady=5)

    tk.Label(edit_dialog, text="Number of Pictures:").pack(pady=5)
    pictures_edit = tk.Entry(edit_dialog)
    pictures_edit.insert(0, current_values[1])
    pictures_edit.pack(pady=5)

    # Save button
    def save_changes():
        try:
            wavelength = wavelength_edit.get()
            num_pictures = pictures_edit.get()

            # Validate inputs
            if not wavelength or not num_pictures:
                messagebox.showerror("Error", "Both fields are required.")
                return

            # Update the row
            tree.item(item_id, values=(wavelength, num_pictures))
            edit_dialog.destroy()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save changes: {e}")

    save_button = tk.Button(edit_dialog, text="Save", command=save_changes)
    save_button.pack(pady=10)


def delete_selected_row():
    """Delete the currently selected row"""
    selected_items = tree.selection()
    if not selected_items:
        messagebox.showwarning("Warning", "Please select a row to delete.")
        return

    # Confirm deletion
    confirm = messagebox.askyesno("Confirm Deletion", "Are you sure you want to delete this row?")
    if confirm:
        for item_id in selected_items:
            tree.delete(item_id)


def toggle_image_selection(index, var):
    if var.get():  # If the checkbox is checked
        if index not in selected_images:
            selected_images.append(index)
    else:  # If the checkbox is unchecked
        if index in selected_images:
            selected_images.remove(index)

    logging.info(f"Selected Images: {selected_images}")

    # Enable or disable buttons depending on selections
    if selected_images:
        sum_cubes_button.config(state="normal")
        average_cubes_button.config(state="normal")
        view_selected_button.config(state="normal")
    else:
        sum_cubes_button.config(state="disabled")
        average_cubes_button.config(state="disabled")
        view_selected_button.config(state="disabled")


def filter_images():
    global selected_images

    # Store currently selected wavelength indices before filtering
    selected_wavelengths = set()
    for idx in selected_images:
        if idx < len(loaded_cubes):
            _, _, wavelength, _, _ = loaded_cubes[idx]
            selected_wavelengths.add(wavelength)

    # Clear current selections
    selected_images = []
    sum_cubes_button.config(state="disabled")
    average_cubes_button.config(state="disabled")
    view_selected_button.config(state="disabled")

    selected_wavelength = wavelength_filter.get()

    # Clear the current image panel
    for widget in image_panel_frame.winfo_children():
        widget.destroy()

    # Create a frame that will contain the grid of images
    image_grid_frame = tk.Frame(image_panel_frame)
    image_grid_frame.pack(fill=tk.BOTH, expand=True)

    # Number of columns in the grid
    num_columns = 4  # Adjust this based on your preferred layout
    current_row = 0
    current_column = 0

    # Filter and display images according to selection
    filtered_cubes = []

    # Create filtered list based on selection
    if selected_wavelength == 'No Filter':
        filtered_cubes = list(enumerate(loaded_cubes))  # Include all cubes with their indices
    else:
        # Only include cubes matching the selected wavelength
        for idx, (cube, _, wavelength, i, _) in enumerate(loaded_cubes):
            if wavelength == selected_wavelength:
                filtered_cubes.append((idx, (cube, _, wavelength, i, _)))

    # Display the filtered cubes in a grid
    for idx, (cube, meta, wavelength, i, output_rgb_image) in filtered_cubes:
        if os.path.exists(output_rgb_image):
            img = Image.open(output_rgb_image)
            img = img.resize((200, 150), Image.Resampling.LANCZOS)
            img_tk = ImageTk.PhotoImage(img)

            # Store the image to prevent garbage collection
            loaded_images.append(img_tk)

            # Create a frame for each image, its label, and checkbox
            image_frame = tk.Frame(image_grid_frame)
            image_frame.grid(row=current_row, column=current_column, padx=5, pady=5, sticky='nsew')

            # Display the image in the frame
            img_label = tk.Label(image_frame, image=img_tk)
            img_label.pack()

            # Create a variable to track the checkbox state
            checkbox_var = tk.BooleanVar()

            # Set initial state based on previous selection (if wavelength was selected)
            if wavelength in selected_wavelengths:
                checkbox_var.set(True)
                selected_images.append(idx)

            # Create a checkbox next to the image name and make it selectable
            checkbox = tk.Checkbutton(image_frame, text=f'{wavelength}_{i}', variable=checkbox_var,
                                      onvalue=True, offvalue=False,
                                      command=lambda idx=idx, var=checkbox_var: toggle_image_selection(idx, var))
            checkbox.pack(pady=5)

            # Update grid position for next image
            current_column += 1
            if current_column >= num_columns:
                current_column = 0
                current_row += 1

    # Enable buttons if there are selections after filtering
    if selected_images:
        sum_cubes_button.config(state="normal")
        average_cubes_button.config(state="normal")
        view_selected_button.config(state="normal")


def view_selected_cubes():
    if not selected_images:
        messagebox.showinfo("Selected Cubes", "No cubes are currently selected.")
        return

    # Create popup window
    popup = tk.Toplevel(root)
    popup.title("Selected Cubes")
    popup.geometry("400x300")
    popup.transient(root)  # Make dialog modal

    # Create a frame for the list
    frame = tk.Frame(popup)
    frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    # Add a header
    tk.Label(frame, text="Currently Selected Cubes:", font=("Arial", 12, "bold")).pack(anchor="w", pady=(0, 10))

    # Create a scrollable text area for the list
    text_area = tk.Text(frame, height=10, width=40, wrap=tk.WORD)
    text_area.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # Add a scrollbar
    scrollbar = tk.Scrollbar(frame, command=text_area.yview)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    text_area.config(yscrollcommand=scrollbar.set)

    # Populate the text area with selected cube information
    for idx in selected_images:
        if idx < len(loaded_cubes):
            _, _, wavelength, i, _ = loaded_cubes[idx]
            text_area.insert(tk.END, f"• Cube {idx + 1}: Wavelength {wavelength}, Image {i}\n")

    text_area.config(state=tk.DISABLED)  # Make text area read-only

    # Add a close button
    tk.Button(popup, text="Close", command=popup.destroy).pack(pady=10)

    # Center the popup window
    popup.update_idletasks()
    width = popup.winfo_width()
    height = popup.winfo_height()
    x = (popup.winfo_screenwidth() // 2) - (width // 2)
    y = (popup.winfo_screenheight() // 2) - (height // 2)
    popup.geometry(f"{width}x{height}+{x}+{y}")


def select_by_wavelength():
    wavelength = wavelength_select_combobox.get()

    if wavelength == 'Select Wavelength':
        messagebox.showinfo("Selection", "Please select a wavelength first.")
        return

    # Clear previous selections
    global selected_images
    selected_images = []

    # Find all cubes with the selected wavelength
    for idx, (_, _, cube_wavelength, _, _) in enumerate(loaded_cubes):
        if cube_wavelength == wavelength:
            selected_images.append(idx)

    logging.info(f"Auto-selected {len(selected_images)} cubes with wavelength {wavelength}")

    # Update the UI to reflect the selected cubes
    update_selection_ui()

    # Enable action buttons if there are selections
    if selected_images:
        sum_cubes_button.config(state="normal")
        average_cubes_button.config(state="normal")
        view_selected_button.config(state="normal")
        messagebox.showinfo("Selection", f"Selected {len(selected_images)} cubes with wavelength {wavelength}.")
    else:
        sum_cubes_button.config(state="disabled")
        average_cubes_button.config(state="disabled")
        view_selected_button.config(state="disabled")
        messagebox.showinfo("Selection", f"No cubes found with wavelength {wavelength}.")


def update_selection_ui():
    """Update checkbox states to reflect the current selections"""
    # Just recreate the image display with the current selections
    # Store currently selected wavelength indices
    selected_wavelengths = set()
    for idx in selected_images:
        if idx < len(loaded_cubes):
            _, _, wavelength, _, _ = loaded_cubes[idx]
            selected_wavelengths.add(wavelength)

    # Clear the current image panel
    for widget in image_panel_frame.winfo_children():
        widget.destroy()

    # Create a frame that will contain the grid of images
    image_grid_frame = tk.Frame(image_panel_frame)
    image_grid_frame.pack(fill=tk.BOTH, expand=True)

    # Number of columns in the grid
    num_columns = 4  # Adjust this based on your preferred layout
    current_row = 0
    current_column = 0

    # Get current filter state
    current_filter = wavelength_filter.get()

    # Display filtered images in a grid
    for idx, (cube, _, wavelength, i, output_rgb_image) in enumerate(loaded_cubes):
        # Skip images that don't match the filter (if a filter is applied)
        if current_filter != 'No Filter' and wavelength != current_filter:
            continue

        if os.path.exists(output_rgb_image):
            img = Image.open(output_rgb_image)
            img = img.resize((200, 150), Image.Resampling.LANCZOS)
            img_tk = ImageTk.PhotoImage(img)

            # Store the image to prevent garbage collection
            loaded_images.append(img_tk)

            # Create a frame for each image, its label, and checkbox
            image_frame = tk.Frame(image_grid_frame)
            image_frame.grid(row=current_row, column=current_column, padx=5, pady=5, sticky='nsew')

            # Display the image in the frame
            img_label = tk.Label(image_frame, image=img_tk)
            img_label.pack()

            # Create a variable to track the checkbox state
            checkbox_var = tk.BooleanVar()

            # Set initial state based on whether this index is selected
            if idx in selected_images:
                checkbox_var.set(True)

            # Create a checkbox next to the image name and make it selectable
            checkbox = tk.Checkbutton(image_frame, text=f'{wavelength}_{i}', variable=checkbox_var,
                                      onvalue=True, offvalue=False,
                                      command=lambda idx=idx, var=checkbox_var: toggle_image_selection(idx, var))
            checkbox.pack(pady=5)

            # Update grid position for next image
            current_column += 1
            if current_column >= num_columns:
                current_column = 0
                current_row += 1


def update_wavelength_filters():
    sorted_wavelengths = sorted(list(available_wavelengths))

    # Update the filter dropdown
    wavelength_filter['values'] = ['No Filter'] + sorted_wavelengths
    wavelength_filter.set('No Filter')  # Set default to 'No Filter'

    # Update the selection dropdown
    wavelength_select_combobox['values'] = ['Select Wavelength'] + sorted_wavelengths
    wavelength_select_combobox.set('Select Wavelength')  # Set default


def show_combined_image_popup(image_path, summed_cube, metadata):
    popup = tk.Toplevel(root)
    popup.title("Summed Cube - RGB Image")

    # Load and display the RGB image in the popup window
    img = Image.open(image_path)
    img = img.resize((600, 400), Image.Resampling.LANCZOS)  # Resize for display
    img_tk = ImageTk.PhotoImage(img)

    img_label = tk.Label(popup, image=img_tk)
    img_label.image = img_tk  # Keep a reference to avoid garbage collection
    img_label.pack(pady=10)

    # Save RGB button
    save_rgb_button = tk.Button(popup, text="Save RGB", command=lambda: save_rgb(image_path))
    save_rgb_button.pack(side=tk.LEFT, padx=10)

    # Save Cube button
    save_cube_button = tk.Button(popup, text="Save Cube", command=lambda: save_cube(summed_cube, metadata))
    save_cube_button.pack(side=tk.LEFT, padx=10)

    popup.geometry("620x500")
    popup.transient(root)
    popup.grab_set()
    root.wait_window(popup)


def show_averaged_image_popup(image_path, averaged_cube, metadata):
    popup = tk.Toplevel(root)
    popup.title("Averaged Cube - RGB Image")

    # Load and display the RGB image in the popup window
    img = Image.open(image_path)
    img = img.resize((600, 400), Image.Resampling.LANCZOS)  # Resize for display
    img_tk = ImageTk.PhotoImage(img)

    img_label = tk.Label(popup, image=img_tk)
    img_label.image = img_tk  # Keep a reference to avoid garbage collection
    img_label.pack(pady=10)

    # Save RGB button
    save_rgb_button = tk.Button(popup, text="Save RGB",
                                command=lambda: save_rgb_image(image_path, "averaged_rgb_image.png"))
    save_rgb_button.pack(side=tk.LEFT, padx=10)

    # Save Cube button
    save_cube_button = tk.Button(popup, text="Save Cube", command=lambda: save_averaged_cube(averaged_cube, metadata))
    save_cube_button.pack(side=tk.LEFT, padx=10)

    popup.geometry("620x500")
    popup.transient(root)
    popup.grab_set()
    root.wait_window(popup)


def take_snapshot():
    global before_snapshot
    before_snapshot = os.listdir(SAVED_IMAGES_DIRECTORY)
    logging.info(f"Initial snapshot taken: {before_snapshot}")


def open_project_window(new_folders_sorted):
    def select_output_folder():
        selected_folder = filedialog.askdirectory()
        if selected_folder:
            output_path_label.config(text=selected_folder)
            global output_path
            output_path = selected_folder

    def save_project_info():
        global project_name, output_path
        temp_project_name = project_name_entry.get()

        if not temp_project_name or not output_path:
            messagebox.showerror("Error", "Please provide both project name and output path.")
            return

        project_name = temp_project_name  # Set project_name before using it

        if not os.path.exists(output_path):
            os.makedirs(output_path)

        rename_and_copy_folders(new_folders_sorted)
        project_window.destroy()

    project_window = tk.Toplevel(root)
    project_window.title("Project Details")
    project_window.geometry("500x200")

    tk.Label(project_window, text="Project Name:").pack(pady=5)
    project_name_entry = tk.Entry(project_window)
    project_name_entry.pack(pady=5)

    tk.Label(project_window, text="Output Folder:").pack(pady=5)

    output_path_label = tk.Label(project_window, text="No folder selected", relief=tk.SUNKEN, width=40)
    output_path_label.pack(pady=5)
    tk.Button(project_window, text="Browse", command=select_output_folder).pack(pady=5)

    tk.Button(project_window, text="Save", command=save_project_info).pack(pady=10)


def rename_and_copy_folders(new_folders_sorted):
    date_str = datetime.now().strftime("%m-%d")
    current_index = 0

    for child in tree.get_children():
        wavelength = tree.item(child)["values"][0]
        num_pictures = int(tree.item(child)["values"][1])

        for i in range(1, num_pictures + 1):
            new_name = f"{project_name}_{date_str}_{wavelength}_{i}"
            old_folder = os.path.join(SAVED_IMAGES_DIRECTORY, new_folders_sorted[current_index])
            new_folder = os.path.join(output_path, new_name)

            shutil.copytree(old_folder, new_folder)
            logging.info(f"Copied and renamed folder: {old_folder} -> {new_folder}")

            current_index += 1

    messagebox.showinfo("Success", "Folders copied and renamed successfully!")


# ------------------------------
# 7. UI SETUP AND INITIALIZATION
# ------------------------------
def create_right_click_menu():
    """Create right-click context menu for the treeview"""
    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="Edit Row", command=edit_selected_row)
    menu.add_command(label="Delete Row", command=delete_selected_row)
    return menu


def show_popup_menu(event):
    """Show the context menu on right-click"""
    try:
        row_id = tree.identify_row(event.y)
        if row_id:  # If a row was clicked
            # Select the row that was right-clicked
            tree.selection_set(row_id)
            # Display the popup menu
            right_click_menu.post(event.x_root, event.y_root)
    except Exception as e:
        logging.error(f"Error showing popup menu: {e}")


def setup_acquisition_tab(acquisition_frame):
    """Set up the Acquisition tab with all its components"""
    global tree, wavelength_entry, pictures_entry
    global tls_status_label, golden_eye_status_label
    global execute_button, process_button, find_tls_button, find_golden_eye_button
    global right_click_menu, raw_folder_label, acquisition_status_label, load_csv_button

    # Set up the treeview for wavelength and number of pictures
    columns = ("Wavelength", "Number of Pictures")
    tree = ttk.Treeview(acquisition_frame, columns=columns, show="headings")
    tree.heading("Wavelength", text="Wavelength (nm)")
    tree.heading("Number of Pictures", text="Number of Pictures")
    tree.pack(fill=tk.BOTH, expand=True)

    # Initialize the right-click menu
    right_click_menu = create_right_click_menu()

    # Bind right-click event to the treeview
    tree.bind("<Button-3>", show_popup_menu)  # Button-3 is right-click on most systems

    # Device frame for TLS and Golden Eye device status
    device_frame = tk.Frame(acquisition_frame)
    device_frame.pack(pady=10)

    # TLS device controls
    find_tls_button = tk.Button(device_frame, text="Find TLS", command=find_tls)
    find_tls_button.pack(side=tk.LEFT, padx=10)
    tls_status_label = tk.Label(device_frame, text="   ", bg='red', width=2)
    tls_status_label.pack(side=tk.LEFT, padx=5)

    # Golden Eye device controls
    find_golden_eye_button = tk.Button(device_frame, text="Find Golden Eye", command=find_golden_eye)
    find_golden_eye_button.pack(side=tk.LEFT, padx=10)
    golden_eye_status_label = tk.Label(device_frame, text="   ", bg='red', width=2)
    golden_eye_status_label.pack(side=tk.LEFT, padx=5)

    # Raw data folder selection
    raw_folder_frame = tk.Frame(acquisition_frame)
    raw_folder_frame.pack(pady=10)

    select_raw_button = tk.Button(raw_folder_frame, text="Select Raw Data Folder",
                                  command=select_raw_data_folder)
    select_raw_button.pack(side=tk.LEFT, padx=5)

    raw_folder_label = tk.Label(raw_folder_frame, text="Raw folder: Not selected",
                                relief=tk.SUNKEN, width=50)
    raw_folder_label.pack(side=tk.LEFT, padx=5)

    # Input frame for wavelength and number of pictures
    input_frame = tk.Frame(acquisition_frame)
    input_frame.pack(fill=tk.X)

    # Wavelength input
    tk.Label(input_frame, text="Wavelength:").pack(side=tk.LEFT, padx=5, pady=5)
    wavelength_entry = tk.Entry(input_frame)
    wavelength_entry.pack(side=tk.LEFT, padx=5, pady=5)

    # Number of pictures input
    tk.Label(input_frame, text="Number of Pictures:").pack(side=tk.LEFT, padx=5, pady=5)
    pictures_entry = tk.Entry(input_frame)
    pictures_entry.pack(side=tk.LEFT, padx=5, pady=5)

    # Add row button
    add_button = tk.Button(input_frame, text="Add Row", command=add_row)
    add_button.pack(side=tk.LEFT, padx=5, pady=5)

    # Acquisition status label
    acquisition_status_label = tk.Label(acquisition_frame, text="Ready to start acquisition",
                                        relief=tk.SUNKEN, height=2)
    acquisition_status_label.pack(fill=tk.X, padx=10, pady=5)

    # Execute, load CSV, and process buttons
    button_frame = tk.Frame(acquisition_frame)
    button_frame.pack(pady=10)

    execute_button = tk.Button(button_frame, text="Execute Commands",
                               command=execute_commands, state='disabled')
    execute_button.pack(side=tk.LEFT, padx=5)

    load_csv_button = tk.Button(button_frame, text="Load from CSV",
                                command=load_acquisition_from_csv, state='disabled')
    load_csv_button.pack(side=tk.LEFT, padx=5)

    process_button = tk.Button(button_frame, text="Process Results",
                               command=process_results, state='disabled')
    process_button.pack(side=tk.LEFT, padx=5)


def setup_processing_tab(processing_frame):
    """Set up the Processing tab with all its components"""
    global wavelength_filter, wavelength_select_combobox
    global sum_cubes_button, average_cubes_button, view_selected_button
    global image_panel_frame, canvas, progress_label

    # Control Panel (Top section with all controls)
    control_panel = tk.Frame(processing_frame)
    control_panel.pack(fill=tk.X, side=tk.TOP, padx=10, pady=5)

    # Filter Panel (Dropdown and Filter Button)
    filter_panel = tk.Frame(control_panel)
    filter_panel.pack(side=tk.TOP, fill=tk.X, pady=5, anchor='w')

    # Wavelength filter dropdown
    tk.Label(filter_panel, text="Filter by Wavelength:").pack(side=tk.LEFT, padx=5)
    wavelength_filter = ttk.Combobox(filter_panel, state="readonly")
    wavelength_filter.pack(side=tk.LEFT, padx=5)

    # Filter button
    filter_button = tk.Button(filter_panel, text="Filter", command=filter_images)
    filter_button.pack(side=tk.LEFT, padx=10)

    # Selection Panel (Dropdown and Select Button)
    selection_panel = tk.Frame(control_panel)
    selection_panel.pack(side=tk.TOP, fill=tk.X, pady=5, anchor='w')

    # Wavelength selection dropdown
    tk.Label(selection_panel, text="Select by Wavelength:").pack(side=tk.LEFT, padx=5)
    wavelength_select_combobox = ttk.Combobox(selection_panel, state="readonly")
    wavelength_select_combobox.pack(side=tk.LEFT, padx=5)

    # Select button
    select_button = tk.Button(selection_panel, text="Select All", command=select_by_wavelength)
    select_button.pack(side=tk.LEFT, padx=10)

    # Initialize wavelength dropdowns with default values
    wavelength_filter['values'] = ['No Filter']
    wavelength_filter.set('No Filter')
    wavelength_select_combobox['values'] = ['Select Wavelength']
    wavelength_select_combobox.set('Select Wavelength')

    # Load folder button
    load_folder_button = tk.Button(control_panel, text="Load Folder", command=load_folder)
    load_folder_button.pack(side=tk.TOP, anchor='w', pady=5)

    # Progress Label to display how many subfolders have been loaded
    progress_label = tk.Label(control_panel, text="Loaded 0 of 0 subfolders")
    progress_label.pack(side=tk.TOP, anchor='w', pady=5)

    # Create a frame for the canvas and scrollbars
    canvas_frame = tk.Frame(processing_frame)
    canvas_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=5)

    # Create both horizontal and vertical scrollbars
    h_scrollbar = ttk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL)
    h_scrollbar.pack(side=tk.BOTTOM, fill=tk.X)

    v_scrollbar = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL)
    v_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    # Create a scrollable canvas with both scrollbars
    canvas = tk.Canvas(canvas_frame, xscrollcommand=h_scrollbar.set, yscrollcommand=v_scrollbar.set)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # Configure the scrollbars to work with the canvas
    h_scrollbar.config(command=canvas.xview)
    v_scrollbar.config(command=canvas.yview)

    # Frame inside the canvas where images will be displayed in a grid
    image_panel_frame = tk.Frame(canvas)
    canvas.create_window((0, 0), window=image_panel_frame, anchor="nw")

    # Bind the resize_canvas function to the Configure event
    image_panel_frame.bind("<Configure>", resize_canvas)

    # Button frame for action buttons at the bottom
    button_frame = tk.Frame(processing_frame)
    button_frame.pack(side=tk.BOTTOM, pady=10)

    # Action buttons for cube operations
    sum_cubes_button = tk.Button(button_frame, text="Sum Cubes", command=sum_selected_cubes, state="disabled")
    sum_cubes_button.pack(side=tk.LEFT, padx=10)

    average_cubes_button = tk.Button(button_frame, text="Average Cubes", command=average_selected_cubes,
                                     state="disabled")
    average_cubes_button.pack(side=tk.LEFT, padx=10)

    view_selected_button = tk.Button(button_frame, text="View Selected", command=view_selected_cubes, state="disabled")
    view_selected_button.pack(side=tk.LEFT, padx=10)


def resize_canvas(event):
    canvas.configure(scrollregion=canvas.bbox("all"))


# ------------------------------
# 8. APPLICATION ENTRY POINT
# ------------------------------
def main():
    global root, tree, wavelength_entry, pictures_entry, tls_status_label, golden_eye_status_label
    global execute_button, process_button, find_tls_button, find_golden_eye_button
    global wavelength_filter, wavelength_select_combobox
    global sum_cubes_button, average_cubes_button, view_selected_button
    global image_panel_frame, canvas, progress_label, right_click_menu

    # Create main window
    root = tk.Tk()
    root.title("WaveTrigger - Laboratory Equipment Control")
    root.geometry("800x600")

    # Create tab structure
    notebook = ttk.Notebook(root)
    notebook.pack(fill=tk.BOTH, expand=True)

    # Create frames for each tab
    acquisition_frame = tk.Frame(notebook)
    processing_frame = tk.Frame(notebook)

    # Add tabs to the notebook
    notebook.add(acquisition_frame, text="Acquisition")
    notebook.add(processing_frame, text="Processing")

    # Setup tab contents
    setup_acquisition_tab(acquisition_frame)
    setup_processing_tab(processing_frame)

    # Start main loop
    root.mainloop()


if __name__ == "__main__":
    main()