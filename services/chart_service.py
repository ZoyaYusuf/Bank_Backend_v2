from __future__ import annotations

import base64
from datetime import date
from io import BytesIO
from typing import List
import os

# Configure matplotlib for serverless environment
os.environ['MPLBACKEND'] = 'Agg'
import matplotlib
matplotlib.use('Agg')
matplotlib.rcParams['figure.max_open_warning'] = 0

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
 

def generate_rate_chart(
    rate_history: List[dict],
    benchmark: str,
    start_date: str,
    end_date: str,
) -> dict:
    """Render the exact published observations used by the calculation."""
    if not rate_history:
        raise ValueError('No published rate observations are available for charting.')

    observations = [
        (date.fromisoformat(item['date']), float(item['rate']))
        for item in rate_history
    ]
    observations.sort(key=lambda item: item[0])
    dates, rates = zip(*observations)

    figure, axis = plt.subplots(figsize=(10, 4.8), dpi=150)
    try:
        figure.patch.set_facecolor('#071A2B')
        axis.set_facecolor('#0B2440')
        axis.plot(
            dates,
            rates,
            color='#20C997',
            linewidth=2.2,
            marker='o',
            markersize=3.5,
            markeredgewidth=0,
        )
        axis.set_title(f'{benchmark.upper()} Rate Fluctuation', color='white', pad=14, loc='left')
        axis.set_xlabel('Date', color='#CBD5E1', labelpad=10)
        axis.set_ylabel('Reference Rate (%)', color='#CBD5E1', labelpad=10)
        axis.tick_params(colors='#CBD5E1')
        axis.grid(True, color='#94A3B8', alpha=0.18, linewidth=0.7)
        for spine in axis.spines.values():
            spine.set_color('#475569')
        
        # Use simpler date formatting to avoid recursion issues
        axis.xaxis.set_major_locator(mdates.AutoDateLocator())
        axis.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
        
        # Rotate dates without using autofmt_xdate which can cause issues
        plt.setp(axis.xaxis.get_majorticklabels(), rotation=45, ha='right')
        
        figure.tight_layout()

        buffer = BytesIO()
        figure.savefig(buffer, format='png', bbox_inches='tight', facecolor=figure.get_facecolor())
        image = base64.b64encode(buffer.getvalue()).decode('ascii')
    finally:
        plt.close(figure)

    return {
        'image_base64': image,
        'title': f'{benchmark.upper()} Rate Fluctuation',
        'start_date': start_date,
        'end_date': end_date,
        'latest_rate': rates[-1],
        'average_rate': sum(rates) / len(rates),
        'highest_rate': max(rates),
        'lowest_rate': min(rates),
        'observation_count': len(rates),
        'latest_observation_date': dates[-1].isoformat(),
    }
