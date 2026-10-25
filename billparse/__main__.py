from .cli import main

if __name__ == "__main__":      # PDF page workers import this module again (multiprocessing spawn): no second run
    main()
