from ultralytics import YOLO

# Load a model
model = YOLO("../yolov8/yolov8n_custom.pt")  # load a pretrained model (recommended for training)
model.info()

# Use the model
results = model("https://ultralytics.com/images/bus.jpg", verbose=True, save=True)  # predict on an image

