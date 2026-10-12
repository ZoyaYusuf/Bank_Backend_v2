import pandas as pd

# Load the dataset
df = pd.read_csv("data.csv")

# Separate input features (X) and target (y)
X = df.drop("Bankrupt?", axis=1)
y = df["Bankrupt?"]

print("Dataset shape:", df.shape)
print("Features shape:", X.shape)
print("Target shape:", y.shape)

print("\nFirst 5 rows:")
print(X.head())

print("\nTarget distribution:")
print(y.value_counts())