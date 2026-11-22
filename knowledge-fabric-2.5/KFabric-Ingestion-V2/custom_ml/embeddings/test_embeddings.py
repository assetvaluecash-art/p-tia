# -*- coding: utf-8 -*-
"""
Test script for embeddings (offline).

Usage:
    python -m custom_ml.embeddings.test_embeddings "your query text"
    python -m custom_ml.embeddings.test_embeddings  # Uses default test texts
"""

import sys
import time
from typing import List

import numpy as np

from .embeddings_client import EmbeddingsClient


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """Calculate cosine similarity between two vectors."""
    v1 = np.array(vec1)
    v2 = np.array(vec2)
    return float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))


def main():
    """Main test entry point."""
    print("=" * 70)
    print("Embeddings Test")
    print("=" * 70)
    print()
    
    # Get query from command line or use default
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
        print(f"Query: {query}")
    else:
        query = None
        print("No query provided, using default test texts")
    
    print()
    
    # Initialize client
    print("Initializing embeddings client...")
    client = EmbeddingsClient()
    
    # Load model
    print("Loading model...")
    load_start = time.time()
    client.load_model()
    load_time = time.time() - load_start
    print(f"Model loaded in {load_time:.2f}s")
    print()
    
    # Test texts
    if query:
        test_texts = [query]
    else:
        test_texts = [
            "The portfolio shows strong ESG compliance metrics.",
            "Risk assessment for derivative instruments requires careful analysis.",
            "Letter of Credit issuance follows UCP 600 guidelines.",
            "The weather is nice today.",  # Unrelated text for similarity comparison
        ]
    
    # Encode
    print(f"Encoding {len(test_texts)} text(s)...")
    encode_start = time.time()
    vectors = client.encode(test_texts)
    encode_time = time.time() - encode_start
    print(f"Encoding completed in {encode_time:.3f}s")
    print()
    
    # Display results
    print("-" * 70)
    print("Results")
    print("-" * 70)
    
    for i, (text, vec) in enumerate(zip(test_texts, vectors)):
        print(f"\n[{i+1}] Text: {text}")
        print(f"    Dimension: {len(vec)}")
        print(f"    Vector (first 8): {[round(v, 4) for v in vec[:8]]}")
        print(f"    Vector (last 4): {[round(v, 4) for v in vec[-4:]]}")
        print(f"    Norm: {np.linalg.norm(vec):.4f}")
        print(f"    Min/Max: {min(vec):.4f} / {max(vec):.4f}")
    
    # If multiple texts, show similarity matrix
    if len(test_texts) > 1:
        print("\n" + "-" * 70)
        print("Cosine Similarity Matrix")
        print("-" * 70)
        print()
        
        # Header
        header = "     " + "".join(f"  [{i+1}]  " for i in range(len(test_texts)))
        print(header)
        
        for i, vec1 in enumerate(vectors):
            row = f"[{i+1}] "
            for j, vec2 in enumerate(vectors):
                sim = cosine_similarity(vec1, vec2)
                row += f" {sim:.4f} "
            print(row)
    
    # Stats
    print("\n" + "-" * 70)
    print("Statistics")
    print("-" * 70)
    stats = client.get_load_stats()
    print(f"  Device: {stats.get('device', 'unknown')}")
    print(f"  Model path: {stats.get('model_path', 'cache')}")
    print(f"  Load time: {stats.get('load_time_sec', 0):.2f}s")
    print(f"  Encode time: {encode_time:.3f}s")
    print(f"  Texts per second: {len(test_texts) / encode_time:.2f}")
    
    print("\n" + "=" * 70)
    print("TEST PASSED")
    print("=" * 70)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

